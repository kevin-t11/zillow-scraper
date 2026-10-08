#!/usr/bin/env python3
"""Benchmark real address API work, with both caches disabled.

python -m px_bypass.benchmark_addresses --base-url http://127.0.0.1:8000 \
    --count 1000 --concurrency 240 --resolve-concurrency 100
"""

import argparse
import json
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import httpx


def main():
    project = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', default='http://127.0.0.1:8000')
    parser.add_argument('--input', type=Path, default=project / 'input.json')
    parser.add_argument('--expected-zpids', type=Path, help='Optional address-to-ZPID JSON for identity checks')
    parser.add_argument('--count', type=int, default=1000)
    parser.add_argument('--concurrency', type=int, default=240)
    parser.add_argument('--resolve-concurrency', type=int, default=100)
    parser.add_argument('--timeout', type=float, default=300)
    args = parser.parse_args()
    source = json.loads(args.input.read_text())
    addresses = list(dict.fromkeys(a.strip() for a in source if a.strip()))[:args.count]
    if args.count < 1 or len(addresses) != args.count:
        parser.error(f'Need {args.count} distinct addresses; found {len(addresses)}')
    payload = {
        'addresses': addresses, 'concurrency': args.concurrency,
        'resolve_concurrency': args.resolve_concurrency, 'cache_seconds': 0,
    }
    print(f'Fetching {len(addresses)} distinct addresses, caches disabled…', flush=True)
    start = time.monotonic()
    with httpx.Client(timeout=args.timeout) as client:
        response = client.post(args.base_url.rstrip('/') + '/v2/scrape/addresses', json=payload)
        response.raise_for_status()
        data = response.json()
    elapsed = time.monotonic() - start
    results = data['results']
    successful = [r for r in results if r['status'] == 'success']
    unique_zpids = {r['zpid'] for r in successful if r.get('zpid')}
    expected = json.loads(args.expected_zpids.read_text()) if args.expected_zpids else {}
    mismatches = [{'input_address': r['input_address'], 'expected': expected[r['input_address']],
                   'actual': r.get('zpid')}
                  for r in results if r['input_address'] in expected
                  and str(r.get('zpid')) != str(expected[r['input_address']])]
    report = {
        'request_count': len(addresses), 'cache_seconds': 0,
        'request_concurrency': args.concurrency,
        'resolve_concurrency': args.resolve_concurrency,
        'client_elapsed_s': round(elapsed, 2),
        'expected_identity_checked': len(expected),
        'expected_zpid_mismatches': mismatches,
        'response_bytes': len(response.content),
        'results_count': len(results),
        'unique_success_zpids': len(unique_zpids),
        'statuses': dict(Counter(r['status'] for r in results)),
        'failures': [{'input_address': r['input_address'], 'status': r['status'],
                      'error_msg': r.get('error_msg', '')}
                     for r in results if r['status'] != 'success'],
        'all_inputs_in_order': [r['input_address'] for r in results] == addresses,
        'target_20s_met': elapsed < 20 and len(unique_zpids) == args.count
                          and [r['input_address'] for r in results] == addresses and not mismatches,
        'summary': data['summary'],
    }
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    output = project / 'output' / f'addresses-pipeline-{args.count}-{stamp}.summary.json'
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))
    print(f'Saved: {output}')
    if len(unique_zpids) != args.count or not report['all_inputs_in_order'] or mismatches:
        raise SystemExit('Benchmark did not retrieve every distinct listing successfully.')


if __name__ == '__main__':
    main()
