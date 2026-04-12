#!/usr/bin/env python3
"""
Test script for both scraper endpoints.

Usage:
    python test_urls.py urls       # Test URL endpoint with sample URLs
    python test_urls.py addresses  # Test address endpoint with input.json (first 100)
"""

import asyncio
import json
import sys
import time

from app.config import load_settings
from app.proxy import ProxyManager
from app.scraper import scrape_urls_full, scrape_addresses_full


SAMPLE_URLS = [
    "https://www.zillow.com/homedetails/9315-Parrot-Ave-Downey-CA-90240/21091004_zpid/",
    "https://www.zillow.com/homedetails/708-Porter-St-APT-3-Glendale-CA-91205/461600636_zpid/",
    "https://www.zillow.com/homedetails/1947-Palm-Ave-Monterey-Park-CA-91755/20675341_zpid/",
    "https://www.zillow.com/homedetails/1200-Bel-Air-Rd-Los-Angeles-CA-90077/20529664_zpid/",
    "https://www.zillow.com/homedetails/3119-E-Valley-View-Ave-West-Covina-CA-91792/21679730_zpid/",
]


def get_proxy_url():
    settings = load_settings()
    if not (settings.brightdata_customer_id and settings.brightdata_password):
        print("WARNING: No proxy configured — Zillow will likely block requests")
        return None
    pm = ProxyManager.from_settings(settings)
    print(f"Proxy: {settings.brightdata_zone}")
    return pm.get_proxy_url()


def print_summary(results, elapsed, label):
    success = sum(1 for r in results if r.status == "success")
    not_found = sum(1 for r in results if r.status == "not_found")
    errors = sum(1 for r in results if r.status in ("error", "parse_error"))
    with_yb = sum(1 for r in results if r.year_built)
    with_pps = sum(1 for r in results if r.price_per_sqft)
    with_sqft = sum(1 for r in results if r.living_area_sqft)
    with_nearby = sum(1 for r in results if r.nearby_homes)
    unique_zips = len({r.zipcode for r in results if r.zipcode})

    print(f"\n=== {label}: {len(results)} items in {elapsed:.1f}s ===")
    print(f"  Success:          {success}/{len(results)}")
    print(f"  Not found:        {not_found}/{len(results)}")
    print(f"  Errors:           {errors}/{len(results)}")
    print(f"  Unique zipcodes:  {unique_zips}")
    print(f"  year_built:       {with_yb}/{len(results)}")
    print(f"  price_per_sqft:   {with_pps}/{len(results)}")
    print(f"  living_area_sqft: {with_sqft}/{len(results)}")
    print(f"  nearby_homes:     {with_nearby}/{len(results)}")
    print(f"  Rate:             {len(results)/elapsed:.2f} listings/sec")


async def test_urls():
    proxy_url = get_proxy_url()
    print(f"\nScraping {len(SAMPLE_URLS)} sample URLs ...")
    t0 = time.time()
    results = await scrape_urls_full(
        SAMPLE_URLS, proxy_url=proxy_url, concurrency=50, max_retries=2
    )
    elapsed = time.time() - t0
    print_summary(results, elapsed, "URL endpoint")

    output = [r.to_dict() for r in results]
    with open("property_details_100.json", "w") as f:
        json.dump(output, f, indent=2, default=str)
    print(f"\nSaved {len(output)} records to property_details_100.json")


async def test_addresses():
    with open("input.json") as f:
        addresses = json.load(f)[:100]

    proxy_url = get_proxy_url()
    print(f"\nScraping {len(addresses)} addresses from input.json ...")
    t0 = time.time()
    results = await scrape_addresses_full(
        addresses, proxy_url=proxy_url, concurrency=100, max_retries=2
    )
    elapsed = time.time() - t0
    print_summary(results, elapsed, "Address endpoint")

    output = [r.to_dict() for r in results]
    with open("property_details_addresses_100.json", "w") as f:
        json.dump(output, f, indent=2, default=str)
    print(f"\nSaved {len(output)} records to property_details_addresses_100.json")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "urls"
    if mode == "urls":
        asyncio.run(test_urls())
    elif mode == "addresses":
        asyncio.run(test_addresses())
    else:
        print("Usage: python test_urls.py [urls|addresses]")
        sys.exit(1)
