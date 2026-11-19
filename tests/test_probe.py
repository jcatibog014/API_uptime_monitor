"""probe.py: checking one address, timing its parts, and naming what went wrong.

Every check here calls the tests' own little web server (conftest.site) or
fails before leaving the PC, so nothing touches the internet."""
import socket

import pytest

from pulsecheck import probe, targets


def check(url, **kwargs):
    return probe.check(url, allow_private=True, **kwargs)


def fake_dns(*addresses):
    """A resolver that answers with these addresses, like a DNS server would."""
    def resolver(host, port, type=socket.SOCK_STREAM):
        return [(socket.AF_INET, type, 6, "", (address, port)) for address in addresses]
    return resolver


def test_a_good_answer_is_up_with_every_part_timed(site):
    result = check(f"{site}/ok")
    assert result.ok and result.error_kind is None
    assert result.status_code == 200 and result.message == "200 OK"
    assert result.body == b"all good" and result.remote_ip == "127.0.0.1"
    assert result.dns_ms is not None and result.connect_ms is not None and result.ttfb_ms is not None
    assert result.tls_ms is None  # plain http has no TLS step
    assert result.total_ms >= result.ttfb_ms


def test_a_wrong_status_is_down_and_says_why(site):
    result = check(f"{site}/down")
    assert not result.ok and result.error_kind == "http_status"
    assert result.message == "Answered 503 Service Unavailable; expected 200-299."


def test_the_good_answers_can_be_chosen(site):
    assert check(f"{site}/missing", expected="200-299, 404").ok
    assert not check(f"{site}/ok", expected="204").ok


def test_too_slow_is_a_timeout(site):
    result = check(f"{site}/slow?2", timeout=0.4)
    assert not result.ok and result.error_kind == "timeout"
    assert result.message == "No answer within 0.4 s."
    assert result.total_ms < 1500


def test_a_closed_port_is_refused():
    with socket.socket() as spare:
        spare.bind(("127.0.0.1", 0))
        port = spare.getsockname()[1]  # free now, and nobody is listening on it
    result = check(f"http://127.0.0.1:{port}/")
    assert not result.ok and result.error_kind == "refused"


def test_an_unknown_name_is_a_dns_failure():
    def no_such_name(*args, **kwargs):
        raise socket.gaierror("Name or service not known")
    result = check("https://no-such-name.example/", resolver=no_such_name)
    assert not result.ok and result.error_kind == "dns"
    assert result.message == "The name no-such-name.example wasn't found."


def test_redirects_are_followed_and_counted(site):
    result = check(f"{site}/hop")
    assert result.ok and result.redirects == 1 and result.final_url == f"{site}/ok"


def test_a_redirect_loop_stops(site):
    result = check(f"{site}/loop")
    assert not result.ok and result.error_kind == "redirects" and result.redirects == probe.MAX_REDIRECTS


def test_only_the_start_of_a_big_answer_is_read(site):
    result = check(f"{site}/big")
    assert result.ok and len(result.body) == probe.MAX_BODY


def test_head_reads_no_body(site):
    result = check(f"{site}/ok", method="HEAD")
    assert result.ok and result.body == b""


def test_every_request_says_who_is_asking(site):
    """The User-Agent tells a site's owner what keeps calling them."""
    check(f"{site}/ok?who")
    assert ("GET", "/ok?who", probe.USER_AGENT) in site.seen


# ---------- The private-network guard, at check time ----------

def test_a_name_that_points_inside_is_never_called():
    """The name looks public, but its DNS answer is a private address: no connection is made."""
    result = probe.check("https://looks-public.example/", resolver=fake_dns("10.0.0.7"))
    assert not result.ok and result.error_kind == "blocked"
    assert result.connect_ms is None


@pytest.mark.parametrize("address", ["127.0.0.1", "169.254.169.254", "192.168.1.1", "100.64.0.1", "::1",
                                     "fd00::1"])
def test_private_answers_are_blocked(address):
    result = probe.check("http://sneaky.example/", resolver=fake_dns(address))
    assert result.error_kind == "blocked", address


def test_only_allowed_addresses_are_tried():
    """A name with a private and a documentation-range address (not public either) is refused."""
    result = probe.check("http://mixed.example/", resolver=fake_dns("10.0.0.1", "192.0.2.10"))
    assert result.error_kind == "blocked"


def test_a_public_page_cant_redirect_inward(site, monkeypatch):
    """/inward redirects to http://10.1.2.3/. The test server is on 127.0.0.1, so for this
    test 127.0.0.1 counts as public; the redirect's private address is still refused."""
    real = targets.allowed_ip
    monkeypatch.setattr(targets, "allowed_ip",
                        lambda ip, allow_private=False: ip == "127.0.0.1" or real(ip, allow_private))
    result = probe.check(f"{site}/inward")  # allow_private is off
    assert result.redirects == 1 and result.error_kind == "blocked"


def test_statuses_are_read_like_people_write_them():
    assert probe.parse_statuses("200-299") == ((200, 299),)
    assert probe.parse_statuses(" 200, 204 ,301-302") == ((200, 200), (204, 204), (301, 302))
    for bad in ("", "abc", "99", "600", "300-200", "200-"):
        assert probe.statuses_problem(bad), bad


def test_a_certificate_date_is_kept():
    result = probe.Result()
    probe.read_certificate(result, {"notAfter": "Mar  1 12:00:00 2027 GMT",
                                    "issuer": ((("countryName", "US"),), (("organizationName", "Let's Encrypt"),))})
    assert result.cert_expires_at.isoformat() == "2027-03-01T12:00:00"
    assert result.cert_issuer == "Let's Encrypt"
