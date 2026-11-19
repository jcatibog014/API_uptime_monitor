"""Checking one address, and timing every part of the answer.

Why not the requests library? It can't tell us where the time went, and it
looks the server's name up by itself (so targets.py couldn't check the
address it really connects to). Python's own socket, ssl and http.client
can do both, one step at a time:

    DNS        look the name up (and check the address is public: targets.py)
    connect    open a TCP connection to that exact address
    TLS        the secure handshake (https only); we keep the certificate's expiry
    wait       send the request; time until the first byte of the answer
               ("time to first byte": mostly the server thinking)
    total      all of it, including reading up to 64 KB of the body

Each step can fail in its own way, and the kind of failure (error_kind) is
what Pulse Check reports: a DNS failure is a very different problem from a
503. check() never raises: every outcome, good or bad, is a Result.
"""
import http.client
import socket
import ssl
import time
from dataclasses import dataclass, field
from functools import lru_cache
from datetime import UTC, datetime
from http import HTTPStatus
from urllib.parse import urljoin, urlsplit

from . import targets

USER_AGENT = "PulseCheck/1.0 (uptime monitor)"
MAX_BODY = 64 * 1024  # bytes of the answer we read: enough to look for a word, never a whole download
MAX_REDIRECTS = 5
REDIRECTS = {301, 302, 303, 307, 308}
METHODS = ("GET", "HEAD", "POST")


@dataclass
class Result:
    ok: bool = False
    error_kind: str | None = None  # dns, blocked, refused, timeout, tls, connection, redirects, http_status
    message: str = ""
    status_code: int | None = None
    remote_ip: str | None = None
    dns_ms: float | None = None
    connect_ms: float | None = None
    tls_ms: float | None = None
    ttfb_ms: float | None = None
    total_ms: float | None = None
    final_url: str = ""
    redirects: int = 0
    body: bytes = b""
    headers: dict = field(default_factory=dict)
    cert_expires_at: datetime | None = None  # naive UTC
    cert_issuer: str = ""
    slow: bool = False  # up, but slower than the monitor's limit (rules.py)

    def add(self, part, milliseconds):
        """Add time to a part. With redirects a part happens more than once: the parts add up."""
        setattr(self, part, (getattr(self, part) or 0) + milliseconds)


class Failure(Exception):
    def __init__(self, kind, message):
        super().__init__(message)
        self.kind, self.message = kind, message


# ---------- Which status codes count as up ----------

@lru_cache(maxsize=256)
def parse_statuses(text):
    """'200-299' or '200, 204, 301-302' -> [(200, 299)] or [(200, 200), (204, 204), (301, 302)].
    Raises ValueError for anything else."""
    ranges = []  # a tuple below: the answer is cached, so it must not be changed by a caller
    for part in (text or "").replace(" ", "").split(","):
        if not part:
            continue
        low, dash, high = part.partition("-")
        low, high = int(low), int(high if dash else low)  # "200-" has a dash and no end: int("") fails
        if not 100 <= low <= high <= 599:
            raise ValueError(part)
        ranges.append((low, high))
    if not ranges:
        raise ValueError("empty")
    return tuple(ranges)


def statuses_problem(text):
    try:
        parse_statuses(text)
    except ValueError:
        return "Use status codes like 200-299, or 200, 204, 301 (each from 100 to 599)."
    return None


def status_ok(code, expected):
    return code is not None and any(low <= code <= high for low, high in parse_statuses(expected))


def status_text(code):
    try:
        return f"{code} {HTTPStatus(code).phrase}"
    except ValueError:
        return str(code)


# ---------- The check ----------

def check(url, *, method="GET", expected="200-299", timeout=10.0, allow_private=False,
          resolver=socket.getaddrinfo, clock=time.perf_counter):
    """Check one address. Always returns a Result (it never raises)."""
    result = Result(final_url=url)
    started = clock()
    deadline = started + timeout
    try:
        _fetch(result, url, method, deadline, timeout, allow_private, resolver, clock)
        if status_ok(result.status_code, expected):
            result.ok, result.message = True, status_text(result.status_code)
        else:
            raise Failure("http_status", f"Answered {status_text(result.status_code)}; expected {expected}.")
    except Failure as failure:
        result.ok, result.error_kind, result.message = False, failure.kind, failure.message
    except Exception as error:  # a check must never crash the crew; say what happened instead
        result.ok, result.error_kind = False, "connection"
        result.message = f"The check failed unexpectedly ({type(error).__name__})."
    result.total_ms = round((clock() - started) * 1000, 1)
    return result


def _fetch(result, url, method, deadline, timeout, allow_private, resolver, clock):
    def left():
        seconds = deadline - clock()
        if seconds <= 0:
            raise Failure("timeout", f"No answer within {timeout:g} s.")
        return seconds

    def since(moment):
        return round((clock() - moment) * 1000, 1)

    for hop in range(MAX_REDIRECTS + 1):
        parts = urlsplit(url)
        https = parts.scheme == "https"
        host = parts.hostname
        port = parts.port or (443 if https else 80)
        path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")

        # 1. DNS: look the name up ourselves, and keep only addresses we may call.
        moment = clock()
        try:
            answers = resolver(host, port, type=socket.SOCK_STREAM)
        except (socket.gaierror, UnicodeError):
            raise Failure("dns", f"The name {host} wasn't found.") from None
        result.add("dns_ms", since(moment))
        addresses = [answer[4][0] for answer in answers]
        allowed = [address for address in addresses if targets.allowed_ip(address, allow_private)]
        if not allowed:
            raise Failure("blocked", targets.BLOCKED_MESSAGE)
        ip = allowed[0]
        result.remote_ip = ip

        # 2. Connect to THAT address (never to the name again: DNS rebinding).
        moment = clock()
        sock = socket.socket(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.settimeout(left())
            sock.connect((ip, port))
            result.add("connect_ms", since(moment))

            # 3. TLS, checked against the NAME (the certificate is for the name, not the address).
            if https:
                moment = clock()
                context = ssl.create_default_context()
                sock.settimeout(left())
                sock = context.wrap_socket(sock, server_hostname=host)
                result.add("tls_ms", since(moment))
                read_certificate(result, sock.getpeercert())

            # 4. The request, and the time to the first byte of the answer.
            connection_class = http.client.HTTPSConnection if https else http.client.HTTPConnection
            connection = connection_class(host, port, timeout=left())
            connection.sock = sock  # already connected (and checked): http.client won't connect again
            moment = clock()
            sock.settimeout(left())
            connection.request(method, path, headers={"User-Agent": USER_AGENT, "Accept": "*/*",
                                                      "Connection": "close"})
            response = connection.getresponse()
            result.add("ttfb_ms", since(moment))
            sock.settimeout(left())
            body = b"" if method == "HEAD" else response.read(MAX_BODY)
        except Failure:
            raise
        except TimeoutError:
            raise Failure("timeout", f"No answer within {timeout:g} s.") from None
        except ConnectionRefusedError:
            raise Failure("refused", f"{host} refused the connection on port {port}.") from None
        except ssl.SSLCertVerificationError as error:
            raise Failure("tls", f"The certificate isn't valid: {error.verify_message or 'it failed the check'}.") from None
        except ssl.SSLError:
            raise Failure("tls", "The secure connection (TLS) failed.") from None
        except (http.client.HTTPException, OSError) as error:
            raise Failure("connection", f"The connection broke ({type(error).__name__}).") from None
        finally:
            sock.close()

        result.status_code = response.status
        result.headers = {name.lower(): value for name, value in response.getheaders()}
        location = result.headers.get("location")
        if response.status in REDIRECTS and location:
            if hop == MAX_REDIRECTS:
                raise Failure("redirects", f"More than {MAX_REDIRECTS} redirects.")
            url = urljoin(url, location)
            if urlsplit(url).scheme not in targets.SCHEMES:
                raise Failure("redirects", "It redirected to something that isn't a web address.")
            if response.status == 303 or (response.status in (301, 302) and method == "POST"):
                method = "GET"  # what browsers do
            result.redirects += 1
            continue
        result.final_url, result.body = url, body
        return


def check_tcp(host, port, *, timeout=10.0, allow_private=False, resolver=socket.getaddrinfo,
              clock=time.perf_counter):
    """Can we open a connection to this port? (A database, a mail server, a game server: anything
    that isn't a web page.) Up = the connection opened; nothing is sent. Never raises."""
    result = Result(final_url=f"tcp://{host}:{port}")
    started = clock()
    try:
        moment = clock()
        try:
            answers = resolver(host, port, type=socket.SOCK_STREAM)
        except (socket.gaierror, UnicodeError):
            raise Failure("dns", f"The name {host} wasn't found.") from None
        result.dns_ms = round((clock() - moment) * 1000, 1)
        allowed = [answer[4][0] for answer in answers if targets.allowed_ip(answer[4][0], allow_private)]
        if not allowed:
            raise Failure("blocked", targets.BLOCKED_MESSAGE)
        result.remote_ip = allowed[0]
        moment = clock()
        with socket.socket(socket.AF_INET6 if ":" in allowed[0] else socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect((allowed[0], port))
        result.connect_ms = round((clock() - moment) * 1000, 1)
        result.ok, result.message = True, f"Port {port} is open."
    except Failure as failure:
        result.error_kind, result.message = failure.kind, failure.message
    except TimeoutError:
        result.error_kind, result.message = "timeout", f"No answer within {timeout:g} s."
    except ConnectionRefusedError:
        result.error_kind, result.message = "refused", f"{host} refused the connection on port {port}."
    except OSError as error:
        result.error_kind, result.message = "connection", f"The connection failed ({type(error).__name__})."
    result.total_ms = round((clock() - started) * 1000, 1)
    return result


def read_certificate(result, certificate):
    """Keep when the certificate runs out (stage 6 warns before it does) and who issued it."""
    if not certificate:
        return
    not_after = certificate.get("notAfter")
    if not_after:
        result.cert_expires_at = datetime.fromtimestamp(ssl.cert_time_to_seconds(not_after), UTC).replace(tzinfo=None)
    for group in certificate.get("issuer", ()):
        for key, value in group:
            if key == "organizationName":
                result.cert_issuer = value[:120]
