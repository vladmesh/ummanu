"""Full-page screenshots of a running web preview, for judging a layout change by eye.

    <python with playwright> scripts/web_preview_shots.py --base http://127.0.0.1:8797 --out shots/

Playwright is not a dependency of this repository; run this with an interpreter that has it and a
Chromium installed. Each page is shot at 1440 px wide, the board and the card also at 390 px. A card
tab is selected by clicking its label, as a person would; the tabs need no script. The fixed
status bar is drawn at the end of the page rather than over its middle.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

CARD = "/tasks/acme-api-231"
CARD_TABS = ("Task", "Work", "Timeline", "Raw events")

#: `(file stem, path, card tab to select or None, also shot at the mobile width)`.
SHOTS: tuple[tuple[str, str, str | None, bool], ...] = (
    ("dashboard", "/", None, False),
    ("board", "/board", None, True),
    ("sprints", "/sprints", None, False),
    ("sprint-41", "/sprints/sprint:41", None, False),
    *((f"card-{tab.lower().replace(' ', '-')}", CARD, tab, tab in {"Task", "Raw events"}) for tab in CARD_TABS),
)

#: The status bar is fixed to the viewport; in a full-page shot it goes to the end of the page instead.
UNPIN = ".statusbar { position: static !important; }"

DESKTOP = {"width": 1440, "height": 900}
MOBILE = {"width": 390, "height": 844}


def shoot(base: str, out: Path) -> list[str]:
    """Save every shot under `out` and return the URLs visited, in order."""
    out.mkdir(parents=True, exist_ok=True)
    visited = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            for width, viewport in (("1440", DESKTOP), ("390", MOBILE)):
                page = browser.new_page(viewport=viewport, color_scheme="dark")
                for stem, path, tab, mobile in SHOTS:
                    if width == "390" and not mobile:
                        continue
                    url = base.rstrip("/") + path
                    page.goto(url, wait_until="load")
                    page.add_style_tag(content=UNPIN)
                    if tab is not None:
                        page.locator(".tab-bar label", has_text=tab).first.click()
                    page.screenshot(path=str(out / f"{stem}-{width}.png"), full_page=True)
                    visited.append(f"{url}{' [' + tab + ']' if tab else ''} -> {stem}-{width}.png")
                page.close()
        finally:
            browser.close()
    return visited


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", default="http://127.0.0.1:8797")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    for line in shoot(args.base, args.out):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
