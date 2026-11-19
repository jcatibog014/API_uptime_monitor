r"""Make the logo and the app icons (pulsecheck/static/icons/) from the owner's picture,
docs/logo/pulse-check.png: a glowing green heartbeat on a glossy navy tile with round
corners (1024 x 1024, the corners see-through).

This script draws, through Playwright + Edge (a browser scales a picture smoothly):
- logo.png (256 px) for the pages: the menu and the login (shown by logo() in _icons.html);
- icon-32/192/512.png and favicon.ico: the tile as it is, round corners and all;
- apple-touch-icon.png: square, because iPhones round the corners themselves. The tile is
  drawn a little bigger on a navy square of its own colours, so no corner shows;
- icon-maskable-512.png: square too, the tile at 80% on the same navy, because
  Android crops these into its own shape (a circle, a squircle...) and the heartbeat must
  stay inside the middle 80%.

Playwright isn't in requirements.txt: borrow another project's:

    cd C:\Users\USER\Documents\Workspace\API_uptime_monitor
    ..\expense_tracker\.venv\Scripts\python docs\make_icons.py
"""
import base64
import struct
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).parent.parent
SOURCE = ROOT / "docs" / "logo" / "pulse-check.png"
ICONS = ROOT / "pulsecheck" / "static" / "icons"
NAVY = "linear-gradient(180deg, #2b3762 0%, #1a2142 50%, #0d1222 100%)"  # the tile's own colours, top to bottom

# (file, size, square on navy?, how big the tile is drawn: 1 = the whole picture)
PNGS = [
    ("logo.png", 256, False, 1.0),
    ("icon-32.png", 32, False, 1.0),
    ("icon-192.png", 192, False, 1.0),
    ("icon-512.png", 512, False, 1.0),
    ("apple-touch-icon.png", 180, True, 1.16),  # big enough that the round corners fall outside
    ("icon-maskable-512.png", 512, True, 0.8),  # the tile fills the safe circle; its corners fall outside
]
ICO_SIZES = (16, 32, 48)


def html(size, square, scale):
    picture = base64.b64encode(SOURCE.read_bytes()).decode()
    background = NAVY if square else "transparent"
    inner = size * scale
    return (f'<style>html, body {{ margin: 0; background: transparent; }}'
            f'#icon {{ width: {size}px; height: {size}px; overflow: hidden; position: relative; background: {background}; }}'
            f'#icon img {{ position: absolute; width: {inner}px; height: {inner}px; left: {(size - inner) / 2}px;'
            f' top: {(size - inner) / 2}px; }}</style>'
            f'<div id="icon"><img src="data:image/png;base64,{picture}" alt=""></div>')


def render(browser, size, square=False, scale=1.0):
    page = browser.new_page(viewport={"width": size, "height": size})
    page.set_content(html(size, square, scale))
    page.wait_for_function("document.querySelector('#icon img').complete")
    png = page.locator("#icon").screenshot(omit_background=True)
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
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="msedge")
        for name, size, square, scale in PNGS:
            (ICONS / name).write_bytes(render(browser, size, square, scale))
            print("saved", name)
        write_ico(ICONS / "favicon.ico", [(size, render(browser, size)) for size in ICO_SIZES])
        print("saved favicon.ico")
        browser.close()


if __name__ == "__main__":
    main()
