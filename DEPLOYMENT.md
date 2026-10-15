# Deploy to Lightsail

Deployment is manual: push a commit to GitHub, upload that same commit over
SSH, test it on the VM, then restart the service. A GitHub push alone does not
deploy the application. Run one deployment at a time.

| Setting | Value |
|---|---|
| Repository / branch | [kevin-t11/zillow-scraper](https://github.com/kevin-t11/zillow-scraper) / `main` |
| SSH target | `ubuntu@3.210.35.141` |
| SSH key on Kevin's Mac | `/Users/kevin/.ssh/zillow_cloud.pem` |
| Application | `/home/ubuntu/zillow-scraper` |
| Service / Python | `zillow` / `/home/ubuntu/zillow-scraper/venv/bin/python` |
| Public API | `http://3.210.35.141` |

This guide updates the existing VM. Its `.env`, nginx, and systemd configuration
remain in place. Keep credentials in the VM's `.env`. The configured pool uses
24 property-detail HTTP sessions and 10 requests per session; the default path needs no browser.
Install the pinned `curl_cffi` dependency from `requirements.txt`: property details
require its Chrome-compatible connection profile, not just a Chrome User-Agent. Discovery uses a separate pool of 4 search-validated Evomi sessions (`SEARCH_POOL_SIZE`).
Original source history is retained from `ushixan/zillow-scraper`.

## 1. Test and push — on your Mac

```bash
cd /Users/kevin/Depot/Projects/zillow_scraper
git status --short
.venv/bin/python -m unittest discover -s tests -q
git diff --check
```

Refresh the recorded runtime hashes before committing:

```bash
.venv/bin/python - <<'PY'
import hashlib, json
from datetime import date
from pathlib import Path
p = Path('deploy/runtime-manifest.json')
m = json.loads(p.read_text())
for name in m['sha256']:
    m['sha256'][name] = hashlib.sha256(Path(name).read_bytes()).hexdigest()
m['verified_date'] = date.today().isoformat()
p.write_text(json.dumps(m, indent=2) + '\n')
PY
```

Review and stage the intended changes, then push. Skip `git add` and `git commit`
if the release is already committed.

```bash
git add app px_bypass tests deploy run.py requirements.txt README.md API.md DEPLOYMENT.md
git commit -m "Describe your change"
git push origin main
git status --short
git rev-parse HEAD
git ls-remote origin refs/heads/main
```

Continue when the working tree is clean and the two commit hashes match.

## 2. Upload — on your Mac

The archive contains committed application files. It excludes `.env`, local
virtual environments, saved sessions, and benchmark output.

```bash
git archive --format=tar.gz --output=/tmp/zillow-release.tar.gz HEAD \
  app px_bypass tests deploy run.py requirements.txt README.md API.md DEPLOYMENT.md
git rev-parse HEAD > /tmp/zillow-commit.txt
scp -i /Users/kevin/.ssh/zillow_cloud.pem \
  /tmp/zillow-release.tar.gz /tmp/zillow-commit.txt ubuntu@3.210.35.141:/tmp/
ssh -i /Users/kevin/.ssh/zillow_cloud.pem ubuntu@3.210.35.141
```

Run the remaining steps in the same SSH terminal. If any command fails, resolve
the error before continuing.

## 3. Stage and test — on Lightsail

Build a separate Python environment for this release. This keeps dependency
changes away from the running service and allows code and dependencies to roll
back together. Keep this release folder after deployment.

```bash
set -e
APP=/home/ubuntu/zillow-scraper
RELEASE=/home/ubuntu/zillow-deploy-backups/$(date -u +%Y%m%dT%H%M%SZ)
mkdir -p "$RELEASE/stage" "$RELEASE/previous"
tar -xzf /tmp/zillow-release.tar.gz -C "$RELEASE/stage"
cp /tmp/zillow-commit.txt "$RELEASE/commit.txt"
python3 -m venv "$RELEASE/venv"
"$RELEASE/venv/bin/python" -m pip install -r "$RELEASE/stage/requirements.txt"
cd "$RELEASE/stage"
"$RELEASE/venv/bin/python" -m unittest discover -s tests -q
```

If Ubuntu reports that `venv` is unavailable, install `python3-venv` first.

## 4. Back up, install, and restart — on Lightsail

This briefly interrupts the API. Save the printed release path for rollback.
Only the listed code folders are synchronized; the VM's configuration stays in place.

```bash
for item in app px_bypass tests deploy run.py requirements.txt README.md API.md DEPLOYMENT.md; do
  cp -a "$APP/$item" "$RELEASE/previous/"
done
printf 'Release and rollback folder: %s\n' "$RELEASE"
sudo systemctl stop zillow
mv "$APP/venv" "$RELEASE/previous-venv"
ln -s "$RELEASE/venv" "$APP/venv"
for dir in app px_bypass tests deploy; do
  rsync -a --delete "$RELEASE/stage/$dir/" "$APP/$dir/"
done
for file in run.py requirements.txt README.md API.md DEPLOYMENT.md; do
  cp "$RELEASE/stage/$file" "$APP/$file"
done
sudo systemctl start zillow
```

## 5. Verify — on Lightsail

```bash
systemctl is-active zillow
cat "$RELEASE/commit.txt"
cd "$APP"
"$APP/venv/bin/python" - <<'PY'
import hashlib, json
from pathlib import Path
m = json.loads(Path('deploy/runtime-manifest.json').read_text())
bad = [p for p, h in m['sha256'].items()
       if hashlib.sha256(Path(p).read_bytes()).hexdigest() != h]
assert not bad, f'Runtime files differ: {bad}'
print('All recorded runtime hashes match')
PY
curl --silent --show-error http://127.0.0.1:8000/health
```

Startup may take a minute or longer while sessions are validated. For listing
searches, check `search_pool.live` (4 when fully ready). `status: "search-only"`
and HTTP 503 on the combined health endpoint mean detail scraping is unavailable;
listing discovery can still be ready. For startup errors, run
`sudo journalctl -u zillow -n 80 --no-pager`.

Verify the public listings endpoint and its result count:

```bash
"$APP/venv/bin/python" - <<'PYTEST'
import time, httpx
body = {
    "bbox": {"north": 42.1, "south": 41.65, "east": -87.5, "west": -88.4},
    "home_types": ["single_family"], "max_results": 1000,
}
started = time.monotonic()
r = httpx.post('http://3.210.35.141/v2/search/listings', json=body, timeout=90)
r.raise_for_status()
data = r.json()
ids = [item['zpid'] for item in data['results']]
assert len(ids) == len(set(ids)), 'Duplicate listings'
print({'unique': len(ids), 'elapsed_s': round(time.monotonic() - started, 2),
       'search': data['summary']['search']})
PYTEST
```

Check the returned count and coverage flags; available inventory varies. This
endpoint always performs fresh discovery. Do not send `include_details`,
`cache_seconds`, or `concurrency`.

For the separate detail services, wait for `live: 24` before a full-speed batch.
Test both public endpoints with caching disabled:

```bash
curl --fail --silent --show-error --compressed --max-time 180 \
  http://3.210.35.141/v2/scrape/urls \
  -H 'Content-Type: application/json' \
  -d '{"urls":["https://www.zillow.com/homedetails/51816594_zpid/"],"cache_seconds":0}'

curl --fail --silent --show-error --compressed --max-time 180 \
  http://3.210.35.141/v2/scrape/addresses \
  -H 'Content-Type: application/json' \
  -d '{"addresses":["6910 Farragut Ave, Falls Church, VA 22042"],"cache_seconds":0}'
```

Each response should contain one successful listing with ZPID `51816594`, zero
blocks, and zero cache hits. Check the listing result as well as the HTTP status.
See [API.md](API.md) for bulk benchmarks and request settings.

## Rollback — on Lightsail

This rollback applies to releases created by this guide. Set `APP` and `RELEASE`
again if using a new SSH terminal; use the release path saved in step 4. The
first check requires the previous Python environment to have been saved.

```bash
set -e
test -e "$RELEASE/previous-venv/bin/python"
sudo systemctl stop zillow
for dir in app px_bypass tests deploy; do
  rsync -a --delete "$RELEASE/previous/$dir/" "$APP/$dir/"
done
for file in run.py requirements.txt README.md API.md DEPLOYMENT.md; do
  cp "$RELEASE/previous/$file" "$APP/$file"
done
test -L "$APP/venv" && rm "$APP/venv"
mv "$RELEASE/previous-venv" "$APP/venv"
sudo systemctl start zillow
```

Repeat the health and endpoint checks. Retain release folders while the active
environment or a rollback environment still references them.

Earlier benchmarks, deployment details, and backup locations are in
[deployment history](deploy/HISTORY.md).
