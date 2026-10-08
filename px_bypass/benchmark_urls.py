#!/usr/bin/env python3
"""Benchmark fresh URL API fetches and verify every distinct requested ZPID.

python -m px_bypass.benchmark_urls --count 1000 --concurrency 240
"""

import argparse
import json
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import httpx

from .scraper import parse_listing_url


def main():
    project = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', default='http://127.0.0.1:8000')
    parser.add_argument('--input', type=Path, default=project / 'input-urls.json')
    parser.add_argument('--count', type=int, default=1000)
    parser.add_argument('--concurrency', type=int, default=240)
    parser.add_argument('--timeout', type=float, default=300)
    args = parser.parse_args()
    if not 1 <= args.concurrency <= 300 or not 1 <= args.count <= 10000:
        parser.error('concurrency must be 1–300 and count must be 1–10000')
    source = json.loads(args.input.read_text())
    by_zpid = {}
    for candidate in source:
        try:
            _, zpid = parse_listing_url(candidate)
        except ValueError:
            continue
        by_zpid.setdefault(zpid, candidate)
        if len(by_zpid) == args.count:
            break
    if len(by_zpid) != args.count:
        parser.error(f'Need {args.count} distinct ZPIDs; found {len(by_zpid)}')
    urls = list(by_zpid.values())
    payload = {'urls': urls, 'concurrency': args.concurrency, 'cache_seconds': 0}
    print(f'Fetching {len(urls)} distinct URLs with both cache reads and writes disabled…', flush=True)
    start = time.monotonic()
    with httpx.Client(timeout=args.timeout) as client:
        response = client.post(args.base_url.rstrip('/') + '/v2/scrape/urls', json=payload)
        response.raise_for_status()
        data = response.json()
    elapsed = time.monotonic() - start
    results = data['results']
    successful = [r for r in results if r['status'] == 'success']
    actual_zpids = {str(r['zpid']) for r in successful if r.get('zpid')}
    expected_zpids = set(by_zpid)
    complete = (actual_zpids == expected_zpids and len(results) == args.count
                and len(successful) == args.count and data['summary'].get('cached') == 0)
    report = {
        'base_url': args.base_url, 'request_count': args.count,
        'request_concurrency': args.concurrency, 'cache_seconds': 0,
        'client_elapsed_s': round(elapsed, 2), 'response_bytes': len(response.content),
        'content_encoding': response.headers.get('content-encoding', 'identity'),
        'wire_content_length': response.headers.get('content-length'),
        'results_count': len(results), 'unique_success_zpids': len(actual_zpids),
        'statuses': dict(Counter(r['status'] for r in results)),
        'missing_zpids': sorted(expected_zpids - actual_zpids),
        'unexpected_zpids': sorted(actual_zpids - expected_zpids),
        'all_requested_listings_fetched_fresh': complete,
        'target_20s_met': complete and elapsed < 20,
        'summary': data['summary'],
    }
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    output = project / 'output' / f'urls-{args.count}-c{args.concurrency}-{stamp}.summary.json'
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))
    print(f'Saved: {output}')
    if not complete:
        raise SystemExit('Benchmark did not fetch every requested distinct listing fresh.')


if __name__ == '__main__':
    main()
