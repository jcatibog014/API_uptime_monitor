"""targets.py: which addresses a monitor may have (the private-network guard)."""
import pytest

from pulsecheck import targets


@pytest.mark.parametrize("url", ["https://api.example.com/health", "http://example.com", "https://8.8.8.8/",
                                 "https://example.com:8443/status?full=1", "HTTPS://Example.com/"])
def test_public_addresses_are_fine(url):
    assert targets.url_problem(url) is None


@pytest.mark.parametrize("url, problem", [
    ("", "Enter an address"),
    ("example.com", "Start the address with https://"),
    ("ftp://example.com/", "Start the address with https://"),
    ("javascript:alert(1)", "Start the address with https://"),
    ("https://", "no server name"),
    ("https://user:secret@example.com/", "Leave the user name and password out"),
    ("https://example.com:99999/", "port number"),
    ("https://exa mple.com/", "spaces"),
    ("https://" + "a" * 2000 + ".com", "at most 2000"),
])
def test_bad_addresses_are_explained(url, problem):
    assert problem in targets.url_problem(url)


@pytest.mark.parametrize("url", ["http://127.0.0.1:5000/", "http://localhost/", "http://10.0.0.5/",
                                 "http://169.254.169.254/latest/meta-data/", "http://[::1]/", "http://printer.local/",
                                 "http://db.internal:5432/", "http://192.168.0.1/", "http://[::ffff:10.0.0.1]/"])
def test_private_addresses_are_refused(url):
    assert targets.url_problem(url) == targets.BLOCKED_MESSAGE


def test_private_addresses_can_be_allowed_on_your_own_pc():
    assert targets.url_problem("http://127.0.0.1:5011/", allow_private=True) is None


def test_the_scheme_is_saved_in_lowercase():
    assert targets.clean_url("  HTTPS://Example.com/Path ") == "https://Example.com/Path"


def test_the_app_refuses_to_allow_private_addresses_on_render(monkeypatch, tmp_path):
    from pulsecheck import create_app
    monkeypatch.setenv("RENDER", "true")
    monkeypatch.setenv("ALLOW_PRIVATE_TARGETS", "1")
    with pytest.raises(RuntimeError, match="your own PC only"):
        create_app({"SECRET_KEY": "x", "DATABASE_URL": "sqlite:///" + str(tmp_path / "x.sqlite")})
