# Property detail query

`property_details.json` is the persisted GET query used by both address and URL
scraping, including session validation. It retrieves the combined priority and
non-priority property fields in one request. The existing parser accepts the
same `data.property` shape. Missing source fields remain empty.

Verified on 2026-10-12 against Zillow's public JavaScript manifest:

https://www.zillow.com/s3/web-platform/sub-apps/zillow/not-for-sale-page-sub-app/not-for-sale-page-sub-app.prod.main.18068463.155a2f40/web/729.fb730008.bundle.js

The manifest publishes `NotForSaleBotsQuery` with hash
`8a0948d209323316b73b674969f73bf942ab5cb950f1d37a61ef263a41ff06fc`.
Its property fragment combines `NfsPriority_property` and
`NfsNonPriority_property`, preserving photos, description, facts, and histories.
The name describes Zillow's page-rendering query; it also returns active sale
properties when supplied their ZPIDs.

If this identifier stops working, inspect the current public property page's
sub-app version and its published query manifest. Update the template only after
verifying the exact property identity and detail fields for active and sold
properties. Run the regression suite and uncached checks for both scrape routes
before deployment. Do not replace complete records with search previews.

The older `for_sale_non_priority.json` POST template is retained for historical
reference and is not loaded by the runtime. A failed persisted query must not
make a session healthy: validation uses the same request as the scraper and
requires the requested ZPID in `data.property`.
