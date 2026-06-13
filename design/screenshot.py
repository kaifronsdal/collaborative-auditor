"""Screenshot all mockups with Playwright. Usage: uv run --with playwright python screenshot.py"""

from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).parent
OUT = ROOT / "screenshots"
OUT.mkdir(exist_ok=True)

with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 1600, "height": 1000})
    for subdir, prefix in [("mockups", ""), ("mockups-v3", "v3_"), ("mockups-v4", "v4_")]:
        for html in sorted((ROOT / subdir).glob("*.html")):
            page.goto(f"file://{html.resolve()}")
            page.wait_for_timeout(200)
            page.screenshot(path=OUT / f"{prefix}{html.stem}.png")
            page.screenshot(path=OUT / f"{prefix}{html.stem}_full.png", full_page=True)
            print(f"shot {prefix}{html.stem}")
    browser.close()
