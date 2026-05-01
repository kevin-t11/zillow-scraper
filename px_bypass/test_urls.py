#!/usr/bin/env python3
"""Bulk-URL test with patchright cookie pool (free, no Scrapfly)."""

import asyncio
import json
import sys
import time
from pathlib import Path

from .cookie_pool import CookiePool
from .patchright_minter import PatchrightMinter
from .scraper import scrape_urls_via_px


async def main() -> None:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 2500
    concurrency = int(sys.argv[2]) if len(sys.argv) > 2 else 60
    pool_size = int(sys.argv[3]) if len(sys.argv) > 3 else 8
    per_slot_c = int(sys.argv[4]) if len(sys.argv) > 4 else 5
    skip_direct = "--proxy-only" in sys.argv

    project = Path(__file__).resolve().parents[1]
    pool_urls = json.loads((project / "input-urls.json").read_text())
    urls = (pool_urls * (n // len(pool_urls) + 1))[:n]
    print(f"Scraping {len(urls)} URLs ({len(set(urls))} unique) at c={concurrency}, pool={pool_size}")
    print(f"Started at {time.strftime('%H:%M:%S')}")

    t_mint = time.monotonic()
    minter = await PatchrightMinter.create(headless=True)
    print(f"Browser launched in {time.monotonic()-t_mint:.1f}s")

    t_pool = time.monotonic()
    cookie_pool = await CookiePool.create(minter.mint, pool_size, per_slot_concurrency=per_slot_c)
    print(f"Pool minted in {time.monotonic()-t_pool:.1f}s ({cookie_pool.stats})")

    t0 = time.monotonic()
    try:
        results, stats = await scrape_urls_via_px(
            urls, concurrency=concurrency, max_retries=3,
            skip_direct=skip_direct, pool=cookie_pool,
        )
    finally:
        await cookie_pool.aclose()
        await minter.close()
    elapsed = time.monotonic() - t0

    out = project / "output" / f"urls_{n}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps([r.to_dict() for r in results], indent=2, default=str))

    print()
    print(f"=== {n} URLs in {elapsed:.1f}s ===")
    print(f"  Success:           {stats.success}/{n}")
    print(f"  Blocked (403):     {stats.blocked}/{n}")
    print(f"  Not found (404):   {stats.not_found}/{n}")
    print(f"  Other errors:      {stats.other_error}/{n}")
    print(f"  Rate:              {stats.rate:.2f} listings/sec")
    print()
    print(f"  Pool stats:        {cookie_pool.stats}")
    print(f"  Bytes received:    {stats.bytes_total/1024/1024:.1f} MB")
    print(f"  Output JSON:       {out.stat().st_size/1024/1024:.1f} MB")


if __name__ == "__main__":
    asyncio.run(main())
