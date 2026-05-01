"""
PX-bypass scraper.

A Camoufox-warmed PerimeterX cookie session is replayed via httpx against
Zillow's ForSaleNonPriorityQuery GraphQL endpoint. ~5x less bandwidth per
listing than scraping the full HTML page, and the response includes
schools / price_history / tax_history that BrightData web_unlocker blocks.
"""
