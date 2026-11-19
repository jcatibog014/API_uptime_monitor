"""Which addresses Pulse Check may call.

A monitor makes the SERVER call an address and shows what came back. Without
a guard, anyone with a login could point a monitor at
http://169.254.169.254/ (the cloud's metadata service, which hands out the
server's secret keys) or at a database inside the private network, and read
the answers on the monitor page. That is called SSRF (server-side request
forgery). So:

1. url_problem() checks an address when it is saved: http or https, a server
   name, no password inside the address, nothing that is plainly private.
2. At check time, probe.py looks the name up ITSELF, checks every answer with
   allowed_ip(), and connects to that exact address. Checking the name first
   and then letting a library look it up again would let a tricky DNS server
   answer "public" the first time and "private" the second (DNS rebinding).
3. Every redirect is checked the same way, so a public page can't send us inward.

ALLOW_PRIVATE_TARGETS=1 (your own PC only; the app refuses it on Render)
lets you watch your other apps on 127.0.0.1.
"""
import ipaddress
from urllib.parse import urlsplit

MAX_URL = 2000
SCHEMES = ("http", "https")
BLOCKED_MESSAGE = "That address is inside a private network, so Pulse Check won't call it."
# Names that always mean "this machine" or "this private network".
PRIVATE_NAMES = ("localhost",)
PRIVATE_ENDINGS = (".localhost", ".local", ".internal", ".lan", ".home.arpa")


def allowed_ip(ip_text, allow_private=False):
    """True if Pulse Check may connect to this address."""
    try:
        ip = ipaddress.ip_address(ip_text.split("%")[0])  # drop an IPv6 zone like %eth0
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped  # ::ffff:10.0.0.1 is really 10.0.0.1
    if ip.is_multicast or ip.is_unspecified:
        return False
    if allow_private:
        return True
    # is_global is False for private, loopback, link-local (the metadata service),
    # shared carrier space (100.64.0.0/10), documentation and reserved ranges.
    return ip.is_global


def is_private_name(host):
    host = host.lower().rstrip(".")
    return host in PRIVATE_NAMES or host.endswith(PRIVATE_ENDINGS)


def url_problem(url, allow_private=False):
    """Why this address can't be a monitor's (a sentence), or None if it can."""
    url = (url or "").strip()
    if not url:
        return "Enter an address, like https://api.example.com/health."
    if len(url) > MAX_URL:
        return f"Use at most {MAX_URL} characters."
    if any(character.isspace() for character in url):
        return "An address can't have spaces in it."
    parts = urlsplit(url)
    if parts.scheme.lower() not in SCHEMES:
        return "Start the address with https:// (or http://)."
    if not parts.hostname:
        return "That address has no server name after the //."
    if parts.username or parts.password:
        return "Leave the user name and password out of the address."
    try:
        parts.port  # noqa: B018  (raises ValueError for a port like :99999)
    except ValueError:
        return "That port number isn't right."
    if not allow_private:
        host = parts.hostname
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            literal = None
        if (literal is not None and not allowed_ip(host)) or (literal is None and is_private_name(host)):
            return BLOCKED_MESSAGE
    return None


def clean_url(url):
    """The address as saved: no spaces around it, the scheme in lowercase."""
    url = (url or "").strip()
    scheme, sep, rest = url.partition("://")
    return f"{scheme.lower()}{sep}{rest}" if sep else url
