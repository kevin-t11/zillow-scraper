#!/usr/bin/env python3
"""Bulk address test — resolve via autocomplete, scrape via patchright pool.

    python -m px_bypass.test_addresses 1000 60 8
"""

import asyncio
import json
import sys
import time
from pathlib import Path

from app.models import PropertyListing
from .cookie_pool import CookiePool
from .patchright_minter import PatchrightMinter
from .resolver import resolve_addresses
from .scraper import scrape_urls_via_px


async def main() -> None:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 1000
    concurrency = int(sys.argv[2]) if len(sys.argv) > 2 else 60
    pool_size = int(sys.argv[3]) if len(sys.argv) > 3 else 8

    project = Path(__file__).resolve().parents[1]
    addrs = json.loads((project / "input.json").read_text())[:n]
    print(f"Resolving {len(addrs)} addresses via autocomplete…")

    t0 = time.monotonic()
    resolved = await resolve_addresses(addrs, concurrency=50)
    t_resolve = time.monotonic() - t0
    addr_to_url = {a: r[2] for a, r in zip(addrs, resolved) if r is not None}
    unresolved = [a for a, r in zip(addrs, resolved) if r is None]
    print(f"  resolved {len(addr_to_url)}/{len(addrs)} in {t_resolve:.1f}s "
          f"({len(unresolved)} unresolved)")

    print(f"\nLaunching patchright + minting pool of {pool_size}…")
    minter = await PatchrightMinter.create(headless=True)
    pool = await CookiePool.create(minter.mint, pool_size, per_slot_concurrency=5)
    print(f"  pool ready ({pool.stats})")

    print(f"\nScraping {len(addr_to_url)} URLs at c={concurrency}…")
    t1 = time.monotonic()
    try:
        results, stats = await scrape_urls_via_px(
            list(addr_to_url.values()),
            concurrency=concurrency, max_retries=3,
            skip_direct=True, pool=pool,
        )
    finally:
        await pool.aclose()
        await minter.close()
    t_scrape = time.monotonic() - t1

    url_to_r = {r.url or r.input_address: r for r in results}
    listings: list[PropertyListing] = []
    for addr, url in addr_to_url.items():
        r = url_to_r.get(url)
        if r is not None:
            r.input_address = addr
            listings.append(r)
    for addr in unresolved:
        listings.append(PropertyListing(
            input_address=addr, status="not_found",
            error_msg="autocomplete miss",
        ))

    out = project / "output" / f"addresses_{n}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps([r.to_dict() for r in listings], indent=2, default=str))

    total_elapsed = time.monotonic() - t0
    print()
    print(f"=== {n} addresses in {total_elapsed:.1f}s ===")
    print(f"  Resolve phase:     {t_resolve:.1f}s ({len(addr_to_url)}/{len(addrs)} resolved)")
    print(f"  Scrape  phase:     {t_scrape:.1f}s")
    print(f"  Success:           {stats.success}/{n}")
    print(f"  Blocked (403):     {stats.blocked}/{n}")
    print(f"  Not found:         {stats.not_found + len(unresolved)}/{n}")
    print(f"  Other errors:      {stats.other_error}/{n}")
    print(f"  Scrape rate:       {stats.rate:.2f} listings/sec")
    print(f"  Pool stats:        {pool.stats}")
    print(f"  Output:            {out}")


if __name__ == "__main__":
    asyncio.run(main())
