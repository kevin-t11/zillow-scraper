"""Explicit, uncached end-to-end checks; exits nonzero if any round fails.

Run against a running API, for example:
    python -m px_bypass.check_service --base-url http://127.0.0.1:8000 --rounds 5
This makes real upstream requests. It does not install a recurring job.
"""

import argparse
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import time

import httpx

SAMPLES = [
    ('51816594', '6910 Farragut Ave, Falls Church, VA 22042'),
    ('4343737', '430 Hillandale Dr, Bartlett, IL 60103'),
    ('3660163', '5808 W Waveland Ave, Chicago, IL 60634'),
]


async def check_service(client: httpx.AsyncClient) -> dict:
    report = {'checked_at': datetime.now(timezone.utc).isoformat(), 'checks': []}
    for name, inputs in [('urls', [f'https://www.zillow.com/homedetails/{z}_zpid/' for z, _ in SAMPLES]),
                         ('addresses', [address for _, address in SAMPLES])]:
        entry = {'endpoint': '/v2/scrape/' + name, 'passed': False}
        started = time.monotonic()
        try:
            r = await client.post(entry['endpoint'], json={name: inputs, 'cache_seconds': 0,
                                                          'concurrency': 3, 'max_retries': 3})
            entry['http'] = r.status_code
            data = r.json()
            rows, summary = data.get('results', []), data.get('summary', {})
            entry['cached'] = summary.get('cached', summary.get('listing_cached'))
            entry['actual_ids'] = [str(row.get('zpid', '')) for row in rows]
            entry['expected_ids'] = [zpid for zpid, _ in SAMPLES]
            entry['passed'] = (r.status_code == 200 and entry['actual_ids'] == entry['expected_ids']
                and entry['cached'] == 0 and summary.get('resolve_cached', 0) == 0
                and all(row.get('status') == 'success' and row.get('photos') and row.get('description')
                        for row in rows))
        except Exception as exc:
            entry['error_type'] = type(exc).__name__  # never log credentials or response bodies
        entry['seconds'] = round(time.monotonic() - started, 3)
        report['checks'].append(entry)
    entry = {'endpoint': '/v2/search/listings', 'passed': False}
    try:
        r = await client.post(entry['endpoint'], json={
            'bbox': {'north': 42.1, 'south': 41.65, 'east': -87.5, 'west': -88.4},
            'home_types': ['single_family'], 'max_results': 5,
        })
        ids = [str(row['zpid']) for row in r.json().get('results', [])]
        entry.update(http=r.status_code, count=len(ids),
                     passed=r.status_code == 200 and len(ids) == len(set(ids)) == 5)
    except Exception as exc:
        entry['error_type'] = type(exc).__name__
    report['checks'].append(entry)
    entry = {'endpoint': '/health', 'passed': False}
    try:
        r = await client.get('/health')
        data = r.json()
        entry.update(http=r.status_code, status=data.get('status'), detail_live=data.get('live'),
                     search_live=data.get('search_pool', {}).get('live'),
                     proactive_renewals=data.get('proactive_renewals'),
                     passed=r.status_code == 200 and data.get('status') == 'ok')
    except Exception as exc:
        entry['error_type'] = type(exc).__name__
    report['checks'].append(entry)
    report['passed'] = all(entry['passed'] for entry in report['checks'])
    return report


async def run(args) -> bool:
    reports = []
    async with httpx.AsyncClient(base_url=args.base_url, timeout=120) as client:
        for index in range(args.rounds):
            report = await check_service(client)
            reports.append(report)
            print(json.dumps(report), flush=True)
            if args.output:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(json.dumps(reports, indent=2) + '\n')
            if index + 1 < args.rounds:
                await asyncio.sleep(args.interval)
    return all(report['passed'] for report in reports)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', default='http://127.0.0.1:8000')
    parser.add_argument('--rounds', type=int, default=1)
    parser.add_argument('--interval', type=float, default=60)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if not 1 <= args.rounds <= 1000 or args.interval < 5:
        parser.error('rounds must be 1–1000 and interval must be at least 5 seconds')
    raise SystemExit(0 if asyncio.run(run(args)) else 1)


if __name__ == '__main__':
    main()
