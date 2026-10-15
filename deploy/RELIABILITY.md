# Reliability and operating limits

## What can be made dependable

This service uses Evomi residential connections to fetch Zillow website data.
The property query and browser connection profile belong to a changing website;
a successful batch does not establish long-term availability. The October 12
query change and October 15 transport change addressed different upstream
rejections. Neither can guarantee that Zillow will continue accepting requests.

The October 15 reliability update addresses failures controlled by this code:

- Sessions renew before their 25-minute hard expiry. With the production
  24-session pool, renewal starts roughly 3–6 minutes before expiry, staggered
  by slot. A working session continues serving until a replacement validates.
- A failed replacement keeps the existing session if it is still usable. Its
  retry pauses for 60 seconds without pausing other slots. During a complete
  outage, a shared cooldown limits recovery to one probe per 60 seconds.
- Startup validates one restored session before serving. Remaining restored
  sessions validate in the background; unvalidated sessions cannot serve work.
- Late failures from retired sessions cannot invalidate their replacements.
- Address resolution checks the supplied full address, including directions,
  units, state and ZIP. Standard street abbreviations and ZIP+4 are normalized.
  Multiple matching property IDs are rejected. Search fallback checks the
  address instead of accepting the first nearby property.

Use a complete address with city, state and ZIP. Uncertain address matches
return `not_found` with a resolution error; this does not prove the property
is absent from Zillow. Use its exact listing URL when the address is ambiguous.
The exact intermittent mismatch from the earlier deployment was not captured;
these guards fix independently reproduced wrong-address selection paths.

## Verify actual data access

From the repository, with its dependencies installed:

```bash
python -m px_bypass.check_service \
  --base-url http://3.210.35.141 \
  --rounds 3 --interval 30 \
  --output output/service-check.json
```

The command checks three known properties through both detail endpoints with
`cache_seconds: 0`, verifies the exact IDs and full-data fields, checks fresh
search results, and records health. It exits nonzero if any round fails.
It makes real upstream requests and consumes proxy bandwidth. It runs only
when invoked; no background schedule or paid service is installed by this
command. Keep canary properties current if their source records change.

HTTP 200 from `/health` means both pools contain usable sessions. It is not
proof that the next fetch will work. `proactive_renewals` records validated
replacements of still-usable sessions; `renew_before_s` is the base renewal
window before per-slot staggering. Inspect row statuses and expected IDs,
not just HTTP 200 or the total result count.

## Diagnose the failure before changing anything

| Observation | Action |
|---|---|
| Some slots fail while other fresh fetches work | Allow per-slot recovery; inspect success rate and available capacity. |
| All property fetches receive PX 403, but search works | Reproduce one detail request through Evomi; compare the current query and client profile. Restarting cannot guarantee recovery. |
| Proxy authentication fails | Verify the protected environment configuration and account access; changing Zillow cookies does not repair proxy authentication. |
| HTTP 200 contains GraphQL errors or wrong/missing property identity | Treat the query as unavailable; never publish that session or return the record as success. Inspect the current published query. |
| Address returns no match while its URL succeeds | Check the exact address and unit. Do not weaken identity checks to increase success counts. |
| An upstream outage outlasts the request budget | Return an honest failure and retry later; do not label cached or partial data as fresh. |

See [DEPLOYMENT.md](../DEPLOYMENT.md) for staged tests, rollout and rollback.
Changes to dependency versions or query identifiers must pass fresh ID/field
checks before deployment. Do not automatically install an untested query or
browser profile into production.

## Sustained validation and its limits

Regression tests cover failed replacements, cancellation, expiry, outage probe
budgets, background startup validation, independent recovery, ambiguous address
IDs, and unsafe search fallback. Live tests use a temporary 90-second session
TTL to exercise multiple renewals quickly, inject one rejected session, restart
the app, and continue uncached requests. This tests recovery mechanics; it is
not a 24-hour production uptime measurement or a 1,000-listing latency SLA.

For an availability commitment, measure representative fresh workloads over
multiple days, including session rollovers, restarts and source changes, and
agree on a supported upstream source. Define success as the correct requested
property with the required fields, and measure complete response latency.

## Longer-term source options

The current runtime remains Evomi-proxy-only. Evomi documents a separate
[Scraper API](https://docs.evomi.com/scraping-products-instructions/scraper-api/quickstart/)
that can select HTTP or browser rendering. It requires an
[API key](https://docs.evomi.com/scraping-products-instructions/scraper-api/authentication/)
and credits; proxy usernames/passwords are not its authentication mechanism.
No Scraper API key is configured locally, so this product has not been tested
for our Zillow fields, throughput, pricing or availability. Generic HTML
retrieval alone does not establish a supported Zillow data service.

If considering that option, obtain explicit Zillow support, field coverage,
rate/parallelism limits, incident handling and service commitments from Evomi,
then test the same address/URL corpus and complete response latency before
routing production traffic to it. No new product has been enabled or purchased.

A supported listing-data feed is another route to reducing website dependency.
Zillow Group documents [Bridge MLS listing access](https://www.zillowgroup.com/developers/api/mls-broker-data/mls-listings/),
subject to MLS approval and invite-only access. This is not automatically
available and does not establish coverage of every Zillow field. It would need
access approval and a field/market coverage assessment; it is not configured
as a fallback here.

Evomi's [session documentation](https://docs.evomi.com/proxy-instructions/residential-proxies/proxy-sessions/)
also explains that ordinary sticky sessions may change IP for stability.
Switching to a locked session would trade that behavior for failure when a
peer disconnects; it is not a general availability fix.
