r"""Make the logo and the app icons (pulsecheck/static/icons/) from docs/logo/pulse-check.svg:
a heartbeat line on a little monitor screen (512 x 512, round corners).

This script:
- copies it to static/icons/logo.svg, unchanged, for the pages;
- draws every PNG icon from it: icon-32/192/512.png and favicon.ico keep the
  round corners; apple-touch-icon.png is square (iPhones round the corners
  themselves); icon-maskable-512.png is square with the line smaller, because
  Android crops these into its own shape (a circle, a squircle...).

Phones and browsers want PNG pictures in fixed sizes, so this draws the SVG in
a browser at each size, through Playwright + Edge (Playwright isn't in
requirements.txt: borrow another project's):

    cd C:\Users\USER\Documents\Workspace\API_uptime_monitor
    ..\expense_tracker\.venv\Scripts\python docs\make_icons.py
"""
import re
import struct
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).parent.parent
SOURCE = ROOT / "docs" / "logo" / "pulse-check.svg"
ICONS = ROOT / "pulsecheck" / "static" / "icons"


def variant(size, rounded=True, share=1.0):
    """The drawing at a size. Square: no round corners and no bezel ring. share < 1: the
    heartbeat drawn smaller, around the middle (for Android's maskable icon)."""
    svg = SOURCE.read_text(encoding="utf-8")
    svg = svg.replace("<svg ", f'<svg width="{size}" height="{size}" ', 1)
    if not rounded:
        svg = re.sub(r'rx="\d+"', 'rx="0"', svg)
        svg = re.sub(r'<rect x="10" y="10"[^>]*/>', "", svg)  # the ring would show at the square's edge
    if share != 1.0:
        offset = 256 * (1 - share)
        svg = re.sub(r"(<path d=\"M60.*?/>\s*<circle[^>]*/>)",
                     lambda match: f'<g transform="translate({offset:.1f} {offset:.1f}) scale({share})">'
                                   f"{match.group(1)}</g>", svg, flags=re.S)
    return svg


# (file, size, round corners, how big the heartbeat is)
PNGS = [
    ("icon-32.png", 32, True, 1.0),
    ("icon-192.png", 192, True, 1.0),
    ("icon-512.png", 512, True, 1.0),
    ("apple-touch-icon.png", 180, False, 1.0),
    ("icon-maskable-512.png", 512, False, 0.78),  # inside Android's safe circle
]
ICO_SIZES = (16, 32, 48)


def render(browser, svg, size):
    page = browser.new_page(viewport={"width": size, "height": size})
    page.set_content(f'<style>html, body {{ margin: 0; background: transparent; }} svg {{ display: block; }}</style>'
                     f'<div id="icon">{svg}</div>')
    png = page.locator("#icon > svg").screenshot(omit_background=True)
    page.close()
    return png


def write_ico(path, pictures):
    """favicon.ico is a small header, a list, then the PNG files themselves."""
    header = struct.pack("<HHH", 0, 1, len(pictures))
    offset = 6 + 16 * len(pictures)
    entries, data = b"", b""
    for size, png in pictures:
        entries += struct.pack("<BBBBHHII", size, size, 0, 0, 1, 32, len(png), offset + len(data))
        data += png
    path.write_bytes(header + entries + data)


def main():
    ICONS.mkdir(parents=True, exist_ok=True)
    (ICONS / "logo.svg").write_text(SOURCE.read_text(encoding="utf-8"), encoding="utf-8")
    print("saved logo.svg")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="msedge")
        for name, size, rounded, share in PNGS:
            (ICONS / name).write_bytes(render(browser, variant(size, rounded, share), size))
            print("saved", name)
        write_ico(ICONS / "favicon.ico", [(size, render(browser, variant(size), size)) for size in ICO_SIZES])
        print("saved favicon.ico")
        browser.close()


if __name__ == "__main__":
    main()
