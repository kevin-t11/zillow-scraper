# Deployed source and configuration

The maintained repository is <https://github.com/kevin-t11/zillow-scraper>.
Both the local checkout and Lightsail checkout originally used
<https://github.com/ushixan/zillow-scraper.git> as their `origin` remote. This
repository retains that source history, including the deployed checkout's base
commit `f2b060e2ee268a8541652181b1f8028ee23857e0` and the subsequent startup fixes.
The original remote is retained locally as `upstream`; availability of that
GitHub repository depends on the account's access.

## Snapshot matching the running service

On 2026-10-08, all runtime source files were compared against
`/home/ubuntu/zillow-scraper` on Lightsail at `3.210.35.141`. The source matches
the running service, including the VM's previously uncommitted
`px_bypass/search.py`, its route and schema, and the address and URL performance
changes. SHA-256 values are recorded in
[`deploy/runtime-manifest.json`](deploy/runtime-manifest.json).

The repository additionally contains the regression tests, reproducible
benchmark tools, and current API documentation. Configuration secrets,
environment backups, saved session cookies, virtual environments, and generated
benchmark output are excluded. Tests do not require proxy credentials or network
access:

```bash
python -m unittest discover -s tests -v
```

All 28 tests passed locally and on the staged VM build. Publishing this snapshot
does not restart the running service or alter its Git checkout.

## Run the deployed performance profile

Install the dependencies from `requirements.txt`, install the browser with
`patchright install chrome`, and supply the required Evomi settings through your
own `.env` file as described in the README. Then run:

```bash
python run.py --pool-size 24 --per-slot-c 10
```

The deployed VM uses 24 sessions, at most 10 concurrent property requests per
session, and mint concurrency 2. Code defaults to 5 requests per session unless
overridden; specifying 10 is necessary to reproduce the tested pool profile.
The live service uses the virtual environment at
`/home/ubuntu/zillow-scraper/venv`, binds to `127.0.0.1:8000`, and runs under the
`zillow` systemd service. The included systemd unit is a template; adjust its
paths and user to match a new deployment.

| Endpoint | Default listing concurrency | Default resolution concurrency |
|---|---:|---:|
| `/v2/scrape/urls` | 240 | Not applicable |
| `/v2/scrape/addresses` | 120 | 100 |
| `/v2/search/listings` | 60 | Search concurrency 4 |

For fresh property retrieval, use `cache_seconds: 0`. Start with one batch of
1,000 inputs, allow 120–180 seconds of client timeout, and enable automatic gzip
decompression. Avoid overlapping large batches on the current VM. Read each
result's status and count distinct successful ZPIDs rather than equating input
rows with unique successful properties. See [API.md](API.md) for full requests,
caching behavior, failure handling, and benchmark commands.

## Post deployment validation

| Endpoint | Successful distinct properties | Request cache hits | VM client elapsed | Server processing |
|---|---:|---:|---:|---:|
| Addresses | 1,000 | 0 | 19.89 seconds | 18.47 seconds |
| URLs | 1,000 | 0 | 34.33 seconds | 32.18 seconds |

Both runs matched every expected property ID and returned no failed rows.
Timings include full response receipt and JSON parsing on the VM, and exclude
external internet transfer and service startup. The URL run included session
refreshes. These are individual observations rather than a latency guarantee.

Live cache population, cache hits, and cache bypass passed for both scrape
endpoints. Search preview and search detail retrieval also passed. The final
health check reported all 24 sessions live. On restart, expired sessions can take
several minutes to replace; in-memory property and resolution caches clear.

The deployment rollback backup remains on the VM at
`/home/ubuntu/zillow-deploy-backups/deploy-20261008T035828Z`. To restore those
previous files on that VM, run the saved `rollback.py` with its virtual
environment Python and restart `zillow`. The backup and credentials are not part
of this repository.
