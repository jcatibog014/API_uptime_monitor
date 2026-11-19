"""The heart-monitor look: the monitor is drawn in CSS and inline SVG, every page wears it,
and the state words always come with their plain meaning."""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from pulsecheck import pulse_words

from .conftest import text

ROOT = Path(__file__).parent.parent / "pulsecheck"
CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")


def test_the_monitor_pieces_are_drawn_in_css():
    for piece in (".screen", ".bezel", ".trace", ".readout", ".pulse-steady", ".pulse-flat", ".title-trace"):
        assert re.search(re.escape(piece) + r"[\s{,:.]", CSS), piece
    assert "url(" not in CSS.replace('url("fonts/', "")  # no picture files, only fonts


def test_dark_mode_is_defined_twice_with_the_same_tokens():
    device = re.search(r"@media \(prefers-color-scheme: dark\) \{\s*:root:not\(\[data-theme=\"light\"\]\) \{(.*?)\n  \}", CSS, re.S).group(1)
    chosen = re.search(r":root\[data-theme=\"dark\"\] \{(.*?)\n\}", CSS, re.S).group(1)
    tokens = lambda block: sorted(re.findall(r"(--[\w-]+):", block))  # noqa: E731
    values = lambda block: sorted(" ".join(block.split()).split(";"))  # noqa: E731
    assert tokens(device) == tokens(chosen)
    assert values(device) == values(chosen)


def test_the_trace_stands_still_for_less_motion():
    assert "prefers-reduced-motion: reduce" in CSS


def test_the_login_has_the_monitor_screen(client):
    page = text(client.get("/login"))
    assert 'class="welcome-panel screen"' in page and 'class="trace"' in page


def test_signed_in_pages_have_the_bezel_menu_and_the_trace(owner):
    client, _ = owner
    page = text(client.get("/dashboard"))
    assert 'class="sidebar bezel"' in page and 'class="sidebar-screen screen"' in page
    assert 'class="title-trace"' in page


def test_every_word_has_a_plain_note():
    for table in (pulse_words.STATE, pulse_words.CHECK):
        for key, word in table.items():
            assert word.note.endswith((".", ")")), key
            assert f".pulse-{word.tone}" in CSS or word.tone == "quiet", key


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js isn't installed")
def test_the_scripts_have_no_syntax_errors():
    for script in (ROOT / "static").glob("*.js"):
        result = subprocess.run(["node", "--check", str(script)], capture_output=True, text=True)
        assert result.returncode == 0, f"{script.name}: {result.stderr}"


def test_no_inline_styles_or_scripts():
    for path in (ROOT / "templates").glob("*.html"):
        source = path.read_text(encoding="utf-8")
        assert 'style="' not in source, path.name
        assert not re.search(r"<script(?![^>]*\bsrc=)", source), path.name
    script = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
    assert "style=" not in script  # the strict CSP blocks inline styles


def test_every_page_sends_the_strict_security_headers(client):
    response = client.get("/login")
    policy = response.headers["Content-Security-Policy"]
    assert "script-src 'self'" in policy and "style-src 'self'" in policy and "frame-ancestors 'none'" in policy
    assert response.headers["X-Content-Type-Options"] == "nosniff"
