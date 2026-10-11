"""
patchright-based cookie minter — drives a patched Chromium through an
Evomi residential proxy, navigates Zillow, and (if PX challenges)
solves press-and-hold by simulating a slow human mouse-down. Returns
the resulting wide-scope cookies for httpx replay.

This is the FREE replacement for Scrapfly. No per-mint cost, just the
Evomi bandwidth used during the page load.

Two entry points:
    cookies = await mint_cookies()                  # one-shot
    minter  = await PatchrightMinter.create()       # reusable, cheap per-mint
    cookies = await minter.mint()
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import time
from urllib.parse import unquote, urlparse

from patchright.async_api import async_playwright, Browser, BrowserContext, Page

from .proxy import EvomiConfig, build_proxy_url, make_session_id

logger = logging.getLogger("px_bypass.patchright_minter")

LISTING_URL = (
    "https://www.zillow.com/homedetails/"
    "6910-Farragut-Ave-Falls-Church-VA-22042/51816594_zpid/"
)

PX_IFRAME_SELECTORS = [
    'iframe[title*="captcha" i]',
    'iframe[src*="captcha"]',
    'iframe[src*="px-captcha"]',
    'iframe[id*="px-captcha"]',
]
PX_HOLD_BUTTON_SELECTORS = [
    '#px-captcha',
    '[aria-labelledby*="px"]',
    'div[role="button"]',
    'button',
]

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/147.0.0.0 Safari/537.36"
)


def _proxy_arg(session_id: str) -> dict:
    evomi = EvomiConfig.from_env()
    proxy_url = build_proxy_url(evomi, session=session_id, country="US", lifetime_min=30)
    p = urlparse(proxy_url)
    return {
        "server": f"{p.scheme}://{p.hostname}:{p.port}",
        "username": unquote(p.username or ""),
        "password": unquote(p.password or ""),
    }


async def _press_and_hold(page: Page, frame_locator) -> bool:
    for sel in PX_HOLD_BUTTON_SELECTORS:
        btn = frame_locator.locator(sel).first
        try:
            await btn.wait_for(state="visible", timeout=3000)
        except Exception:
            continue
        box = await btn.bounding_box()
        if not box:
            continue
        cx = box["x"] + box["width"] / 2 + random.uniform(-3, 3)
        cy = box["y"] + box["height"] / 2 + random.uniform(-3, 3)
        await page.mouse.move(cx + random.uniform(-50, 50), cy + random.uniform(-30, 30))
        await asyncio.sleep(random.uniform(0.2, 0.4))
        await page.mouse.move(cx, cy, steps=20)
        await asyncio.sleep(random.uniform(0.1, 0.3))
        await page.mouse.down()
        t_end = time.monotonic() + random.uniform(9.5, 11.0)
        while time.monotonic() < t_end:
            await page.mouse.move(
                cx + random.uniform(-1, 1), cy + random.uniform(-1, 1), steps=2,
            )
            await asyncio.sleep(0.4)
        await page.mouse.up()
        return True
    return False


async def _solve_in_context(context: BrowserContext, timeout_s: float) -> dict:
    page = await context.new_page()
    try:
        t0 = time.monotonic()
        await page.goto(LISTING_URL, wait_until="domcontentloaded", timeout=30000)
        for sel in PX_IFRAME_SELECTORS:
            fl = page.frame_locator(sel)
            try:
                await fl.locator("body").wait_for(timeout=2000)
                logger.info("PX iframe found (%s) — solving…", sel)
                if await _press_and_hold(page, fl):
                    await asyncio.sleep(2.0)
                    break
            except Exception:
                continue
        # brief settle
        await asyncio.sleep(1.5)
        cookies_list = await context.cookies(["https://www.zillow.com"])
        cookies = {c["name"]: c["value"] for c in cookies_list}
        logger.info("mint elapsed=%.1fs cookies=%d _px3=%s",
                    time.monotonic() - t0, len(cookies), "_px3" in cookies)
        return cookies
    finally:
        await page.close()


class PatchrightMinter:
    """Reusable minter — one shared browser, fresh context per mint."""

    def __init__(self, pw, browser: Browser, *, headless: bool, timeout_s: float):
        self._pw = pw
        self._browser = browser
        self._headless = headless
        self._timeout_s = timeout_s
        self._sem = asyncio.Semaphore(max(1, int(os.getenv("MINT_CONCURRENCY", "8"))))

    @classmethod
    async def create(
        cls, *, headless: bool = True, timeout_s: float = 60.0,
    ) -> "PatchrightMinter":
        pw = await async_playwright().start()
        browser = await pw.chromium.launch(
            headless=headless,
            channel=os.getenv("BROWSER_CHANNEL", "chrome") or None,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ],
        )
        return cls(pw, browser, headless=headless, timeout_s=timeout_s)

    async def mint(self, *, session_id: str | None = None) -> dict:
        session_id = session_id or make_session_id()
        async with self._sem:
            context = await self._browser.new_context(
                proxy=_proxy_arg(session_id),
                user_agent=UA,
                locale="en-US",
                timezone_id="America/Los_Angeles",
                viewport={"width": 1280, "height": 900},
            )
            try:
                return await _solve_in_context(context, self._timeout_s)
            finally:
                await context.close()

    async def close(self) -> None:
        try:
            await self._browser.close()
        finally:
            await self._pw.stop()


async def mint_cookies(*, headless: bool = True, timeout_s: float = 60.0) -> dict:
    """One-shot mint — convenient for tests, expensive for batch."""
    m = await PatchrightMinter.create(headless=headless, timeout_s=timeout_s)
    try:
        return await m.mint()
    finally:
        await m.close()


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    cookies = await mint_cookies(headless=True)
    print(f"\nGot {len(cookies)} cookies. Has _px3: {'_px3' in cookies}")


if __name__ == "__main__":
    asyncio.run(main())
