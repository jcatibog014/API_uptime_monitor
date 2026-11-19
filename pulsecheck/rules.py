"""Smarter checks: what a good answer must say, when it is too slow, and the other kinds of monitor.

A 200 isn't always "working". A health page can answer 200 with
{"status": "degraded"}, or a site can serve its error page with a 200. So an
HTTP monitor may also ask that the answer:
- contains some words ("ok"), or does NOT contain some ("Maintenance");
- has a JSON value at a path (data.db.status = "up"; numbers into lists too:
  items.0.name);
- has a header (x-version).
A failed rule is a failed check (error_kind "assertion"), judged like any other.

slow_ms: an answer that is right but slower than this is a Slow Beat, and the
monitor shows a Weak Pulse (still up: uptime counts it as good).

sleepy: free-plan sites (like Render's) sleep and need up to a minute to wake.
A sleepy monitor waits up to 90 seconds instead of failing a waking site.

Other kinds:
- tcp://host:port   can a connection open (a database, a mail server)?
- heartbeat         nothing is called: YOUR job calls Pulse Check (heartbeat.py),
                    and a missing call is the failure.
"""
import json
from urllib.parse import urlsplit

from . import checking, practice, probe
from .formatting import ms

SLEEPY_TIMEOUT = 90  # seconds
RULE_KEYS = ("contains", "not_contains", "json_path", "json_value", "header")


def json_at(data, path):
    """The value at a dot path ("data.items.0.name"), or raise KeyError."""
    value = data
    for part in path.split("."):
        if isinstance(value, list) and part.isdigit() and int(part) < len(value):
            value = value[int(part)]
        elif isinstance(value, dict) and part in value:
            value = value[part]
        else:
            raise KeyError(part)
    return value


def as_text(value):
    """How a JSON value is compared with what the owner typed: true, 3, ok."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, (dict, list)):
        return json.dumps(value)
    return str(value)


def rule_problem(rules, result):
    """Why the answer breaks the monitor's rules (a sentence), or None."""
    text = result.body.decode("utf-8", errors="replace")
    if rules.get("contains") and rules["contains"] not in text:
        return f'The answer doesn\'t contain "{rules["contains"]}".'
    if rules.get("not_contains") and rules["not_contains"] in text:
        return f'The answer contains "{rules["not_contains"]}".'
    if rules.get("header") and rules["header"].lower() not in result.headers:
        return f'The answer has no {rules["header"]} header.'
    if rules.get("json_path"):
        try:
            value = json_at(json.loads(text), rules["json_path"])
        except ValueError:
            return "The answer isn't JSON."
        except KeyError:
            return f"The answer has nothing at {rules['json_path']}."
        if "json_value" in rules and as_text(value) != rules["json_value"]:
            return f'{rules["json_path"]} is {as_text(value)}, not {rules["json_value"]}.'
    return None


def apply(monitor, result):
    """Check a good answer against the monitor's rules and its speed limit (changes result)."""
    if result.ok and monitor.assertions:
        problem = rule_problem(monitor.assertions, result)
        if problem:
            result.ok, result.error_kind, result.message = False, "assertion", problem
    if result.ok and monitor.slow_ms and result.total_ms and result.total_ms > monitor.slow_ms:
        result.slow = True
        result.message = f"{result.message} Slow: {ms(result.total_ms)}, the limit is {ms(monitor.slow_ms)}."
    return result


def timeout_for(monitor):
    return max(monitor.timeout_s, SLEEPY_TIMEOUT) if monitor.sleepy else monitor.timeout_s


def tcp_address(url):
    """tcp://db.example.com:5432 -> ("db.example.com", 5432)."""
    parts = urlsplit(url)
    return parts.hostname, parts.port


def http_prober(monitor, config, moment):
    result = probe.check(monitor.url, method=monitor.method, expected=monitor.expected_statuses,
                         timeout=timeout_for(monitor), allow_private=config["ALLOW_PRIVATE_TARGETS"])
    return apply(monitor, result)


def tcp_prober(monitor, config, moment):
    host, port = tcp_address(monitor.url)
    return apply(monitor, probe.check_tcp(host, port, timeout=timeout_for(monitor),
                                          allow_private=config["ALLOW_PRIVATE_TARGETS"]))


def practice_prober(monitor, config, moment):
    return apply(monitor, practice.prober(monitor, config, moment))


def heartbeat_prober(monitor, config, moment):
    """A heartbeat monitor's check falls due only when its ping is late: that IS the failure."""
    from .formatting import nice_datetime
    since = f"since {nice_datetime(monitor.last_ping_at, monitor.workspace.time_zone)}" if monitor.last_ping_at else "yet"
    return probe.Result(ok=False, error_kind="missing", message=f"No ping {since}.")


checking.PROBERS.update(http=http_prober, tcp=tcp_prober, practice=practice_prober, heartbeat=heartbeat_prober)
