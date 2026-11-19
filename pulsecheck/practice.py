"""Practice services: pretend APIs that answer inside Pulse Check, for the demo.

A demo must never make our server call real sites (strangers could aim it at
anyone), and it must show trouble: outages, slow answers, a flaky partner.
So a practice monitor's address is practice://<service>, and the answer is
made up here, with no network at all:

- each service has a SCRIPT (always fine, an outage now and then, often slow,
  flaky...);
- the dice are seeded by the service, the monitor and the MOMENT of the
  check, so the same check always gets the same answer. That is what lets
  the demo replay its past 6 hours through the real scheduler and incident
  code, and lets tests say exactly what happens.

A timeout here takes no real time: it reports "No answer within 10 s" at once,
so a replay of hours of checks takes seconds.
"""
import hashlib
import random
from functools import lru_cache
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlsplit

from . import probe

SCHEME = "practice://"
UNIX_START = datetime(1970, 1, 1)


@dataclass(frozen=True)
class Service:
    name: str
    note: str
    fast: tuple  # usual response time range, ms
    slow_share: float = 0.0  # share of answers that are slow
    slow: tuple = (900, 1400)
    flaky_share: float = 0.0  # share of answers that fail on their own (502)
    outage_every: int = 0  # minutes: one outage in each block this long (0 = none)
    outage_length: int = 7  # minutes
    tls: bool = True


SERVICES = {
    "website": Service("Website", "Always up and quick.", (70, 140)),
    "login": Service("Login", "Always up.", (50, 95)),
    "booking-api": Service("Booking API", "Up, with a slow answer now and then.", (120, 260), slow_share=0.04),
    "payments-api": Service("Payments API", "Up, but stops answering for about 7 minutes every few days.",
                            (150, 300), outage_every=3 * 24 * 60),
    "search-api": Service("Search API", "Up, but often slow.", (250, 700), slow_share=0.15, slow=(1500, 2600)),
    "partner-api": Service("Old Partner API", "Flaky: about one answer in sixteen is an error.", (300, 600),
                           flaky_share=0.06),
    "shipping-api": Service("Shipping API", "Down for a while when the demo starts, then back.", (110, 220)),
    "images-cdn": Service("Images CDN", "Up and very quick.", (25, 60)),
}


def choices():
    return [(key, service.name) for key, service in SERVICES.items()]


def address(key, **params):
    query = "&".join(f"{name}={value}" for name, value in params.items())
    return f"{SCHEME}{key}" + (f"?{query}" if query else "")


def parse(url):
    """practice://shipping-api?down=2026-11-19T08:10&for=55 -> ("shipping-api", {"down": ..., "for": ...})."""
    key, params = _parse(url)
    return key, dict(params)


@lru_cache(maxsize=1024)
def _parse(url):  # the same few addresses are read thousands of times in a replay
    parts = urlsplit(url)
    return parts.netloc, tuple((name, values[0]) for name, values in parse_qs(parts.query).items())


def is_practice(url):
    return (url or "").startswith(SCHEME) and parse(url)[0] in SERVICES


@lru_cache(maxsize=4096)
def _stable_number(*parts):
    """A number from text that is the same on every run (Python's hash() changes per process)."""
    return int.from_bytes(hashlib.sha256(":".join(str(part) for part in parts).encode()).digest()[:8], "big")


def in_outage(key, service, params, moment):
    """True if the service's script says it is down at this moment: in the window its
    address names (?down=...&for=minutes), or in one of its regular outages."""
    if "down" in params:
        start = datetime.fromisoformat(params["down"])
        if start <= moment < start + timedelta(minutes=int(params.get("for", "30"))):
            return True
    if "flap" in params:  # down 3 minutes, up 3 minutes, over and over, for a while
        start = datetime.fromisoformat(params["flap"])
        if start <= moment < start + timedelta(minutes=int(params.get("flap_for", "40"))):
            return (moment - start).total_seconds() // 60 % 6 < 3
    if not service.outage_every:
        return False
    minutes = int((moment - UNIX_START).total_seconds() // 60)  # naive UTC: not .timestamp(), which assumes local time
    block = minutes // service.outage_every
    start = block * service.outage_every + _stable_number(key, block) % (service.outage_every - service.outage_length)
    return start <= minutes < start + service.outage_length


def answer(url, moment, monitor_id=0, timeout=10, expected="200-299"):
    """What the practice service says to a check at `moment`, as a probe.Result."""
    key, params = _parse(url)
    params = dict(params)
    service = SERVICES[key]
    dice = random.Random(f"{key}:{monitor_id}:{moment.isoformat()}")
    result = probe.Result(final_url=url)  # no address: it never left Pulse Check
    result.dns_ms = round(dice.uniform(1, 6), 1)
    result.connect_ms = round(dice.uniform(8, 25), 1)
    result.tls_ms = round(dice.uniform(15, 40), 1) if service.tls else None
    before_wait = result.dns_ms + result.connect_ms + (result.tls_ms or 0)
    if in_outage(key, service, params, moment):
        if key != "payments-api":
            result.ttfb_ms = round(dice.uniform(20, 40), 1)
            result.status_code = 503
        else:
            result.error_kind, result.message = "timeout", f"No answer within {timeout:g} s."
            result.ttfb_ms, result.total_ms = None, timeout * 1000.0
            return result
    elif dice.random() < service.flaky_share:
        result.ttfb_ms = round(dice.uniform(40, 120), 1)
        result.status_code = 502
    else:
        low, high = service.slow if dice.random() < service.slow_share else service.fast
        target = dice.uniform(low, high)  # the whole answer; the wait is what the other parts leave
        result.ttfb_ms = round(max(target - before_wait, target * 0.2), 1)
        result.status_code = 200
    result.total_ms = round(before_wait + result.ttfb_ms + dice.uniform(0.5, 3), 1)
    if "cert" in params:  # the script's certificate end date (the demo's Images CDN runs out soon)
        result.cert_expires_at, result.cert_issuer = datetime.fromisoformat(params["cert"]), "Practice CA"
    if result.status_code == 200:  # a small health answer, so content rules have something to read
        result.body = f'{{"status": "ok", "service": "{key}"}}'.encode()
        result.headers = {"content-type": "application/json"}
    if probe.status_ok(result.status_code, expected):
        result.ok, result.message = True, probe.status_text(result.status_code)
    else:
        result.error_kind = "http_status"
        result.message = f"Answered {probe.status_text(result.status_code)}; expected {expected}."
    return result


def prober(monitor, config, moment):
    return answer(monitor.url, moment, monitor.id, monitor.timeout_s, monitor.expected_statuses)
