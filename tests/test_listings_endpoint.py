import asyncio
import math
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI
from pydantic import ValidationError

from app.models import PropertyListing
from px_bypass import routes, search
from px_bypass.cache import TTLCache
from px_bypass.cookie_pool import SessionValidationError

BOUNDS = {'north': 42.0, 'south': 41.0, 'east': -88.0, 'west': -89.0}
LOCATION = search.SearchLocation(
    query='Naperville, IL', display='Naperville, IL', region_id=39931,
    region_type='city', city='Naperville', state='IL', latitude=41.75, longitude=-88.16,
)


def card(zpid, lat=41.75, lon=-88.16, **extra):
    return {'zpid': str(zpid), 'latLong': {'latitude': lat, 'longitude': lon},
            'addressCity': 'Naperville', 'addressState': 'IL', **extra}


def upstream(cards, total=None, pages=1, **extra):
    return {'cat1': {'searchResults': {'listResults': cards, **extra},
                     'searchList': {'totalResultCount': len(cards) if total is None else total,
                                    'totalPages': pages}}}


class SearchValidationTests(unittest.TestCase):
    def test_location_and_bbox_modes(self):
        radius = routes.SearchListingsRequest(location=' Naperville, IL ')
        self.assertEqual(radius.location, 'Naperville, IL')
        self.assertEqual(radius.radius_miles, 12)
        self.assertFalse(radius.strict_location)
        bbox = routes.SearchListingsRequest(bbox=BOUNDS)
        self.assertIsNone(bbox.location)

    def test_invalid_searches_are_rejected(self):
        cases = [
            {}, {'radius_miles': 5}, {'location': '  '},
            {'bbox': BOUNDS, 'location': 'Naperville'},
            {'bbox': BOUNDS, 'radius_miles': 12}, {'bbox': BOUNDS, 'strict_location': True},
            {'bbox': {**BOUNDS, 'north': 91}}, {'bbox': {**BOUNDS, 'north': True}}, {'bbox': {**BOUNDS, 'east': -190}},
            {'bbox': {**BOUNDS, 'north': 41}}, {'bbox': {**BOUNDS, 'west': -80}},
            {'bbox': {**BOUNDS, 'north': math.inf}}, {'bbox': {**BOUNDS, 'west': math.nan}},
            {'bbox': {**BOUNDS, 'typo': 0}}, {'bbox': {'north': 42}},
            {'location': 'Naperville', 'radius_miles': math.nan},
            {'bbox': BOUNDS, 'year_built_min': 2025, 'year_built_max': 2000},
            {'bbox': BOUNDS, 'year_built_min': 0},
            {'bbox': BOUNDS, 'sold_in_last': '30'},
            {'bbox': BOUNDS, 'status': 'sold', 'sold_in_last': '45'},
            {'bbox': BOUNDS, 'status': 'sold', 'new_construction_only': True},
            {'bbox': BOUNDS, 'new_construction_only': True, 'include_new_construction': False},
            {'bbox': BOUNDS, 'year_build_min': 2000},
            {'bbox': BOUNDS, 'max_search_requests': 101},
        ]
        for body in cases:
            with self.subTest(body=body), self.assertRaises(ValidationError):
                routes.SearchListingsRequest(**body)

    def test_every_observed_sold_window_and_one_sided_year_ranges(self):
        for window in ('any', '1', '7', '14', '30', '90', '6m', '12m', '24m', '36m'):
            req = routes.SearchListingsRequest(bbox=BOUNDS, status='sold', sold_in_last=window)
            self.assertEqual(req.sold_in_last, window)
        for years in ({'year_built_min': 2020}, {'year_built_max': 2020}):
            routes.SearchListingsRequest(bbox=BOUNDS, **years)

    def test_new_only_excludes_other_sale_sources(self):
        filters = search.build_filter_state(status='for_sale', home_types=['single_family'],
                                            new_construction_only=True, include_fsbo=True,
                                            include_foreclosures=True, include_auctions=True)
        self.assertTrue(filters['isNewConstruction']['value'])
        for key in ('isForSaleByAgent', 'isForSaleByOwner', 'isAuction', 'isForSaleForeclosure',
                    'isRecentlySold', 'isForRent', 'isComingSoon'):
            self.assertFalse(filters[key]['value'], key)

    def test_sold_and_rent_turn_off_all_sale_statuses(self):
        for status in ('sold', 'for_rent'):
            filters = search.build_filter_state(status=status, home_types=['single_family'],
                                                year_built_min=2000, year_built_max=2025,
                                                sold_in_last='12m' if status == 'sold' else None)
            self.assertEqual(filters['built'], {'min': 2000, 'max': 2025})
            for key in ('isForSaleByAgent', 'isForSaleByOwner', 'isNewConstruction',
                        'isComingSoon', 'isActiveStatus', 'isComingSoonStatus', 'isZillowPreview'):
                self.assertFalse(filters[key]['value'])
            self.assertEqual(filters['isRecentlySold']['value'], status == 'sold')
            self.assertEqual(filters['isForRent']['value'], status == 'for_rent')
            if status == 'sold':
                self.assertEqual(filters['doz'], {'value': '12m'})

    def test_bounds_edges_zero_coordinates_and_circle_corners(self):
        zero = search.parse_search_listing(card(1, 0, 0))
        self.assertEqual((zero.latitude, zero.longitude), (0, 0))
        self.assertIsNone(search.listing_area_rejection(zero, {'north': 1, 'south': 0, 'east': 1, 'west': 0}))
        bounds = search.bounds_for_radius(41.75, -88.16, 10)
        corner = search.parse_search_listing(card(2, bounds['north'], bounds['east']))
        self.assertEqual(search.listing_area_rejection(corner, bounds, (41.75, -88.16), 10), 'out_of_area')
        north = search.parse_search_listing(card(3, bounds['north'], -88.16))
        self.assertIsNone(search.listing_area_rejection(north, bounds, (41.75, -88.16), 10))
        self.assertAlmostEqual(search.distance_miles(0, 0, 1, 0), 69.0934, places=3)

    def test_relaxed_results_excluded_and_map_coordinates_enrich_list_card(self):
        data = upstream([card(1, None, None)], mapResults=[card(1)],
                        relaxedResults=[card(2)], relaxedMapResults=[card(3)])
        listings, total = search.extract_search_listings(data)
        self.assertEqual(total, 1)
        self.assertEqual([x.zpid for x in listings], ['1'])
        self.assertEqual(listings[0].latitude, 41.75)


class SearchDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_bbox_filters_before_limit_and_skips_geocoding(self):
        data = upstream([card(1, 43), card(2, None, None), card(3), card(3)])
        with patch.object(search, 'resolve_search_location', side_effect=AssertionError('geocoding')), \
             patch.object(search, '_fetch_search_payload', AsyncMock(return_value=(data, 1))) as fetch:
            listings, location, stats = await search.discover_zillow_listings(pool=None, bbox=BOUNDS, max_results=1)
        self.assertIsNone(location)
        self.assertEqual([x.zpid for x in listings], ['3'])
        self.assertEqual(stats.discarded_out_of_area, 1)
        self.assertEqual(stats.discarded_missing_coordinates, 1)
        state = fetch.call_args.kwargs['payload']['searchQueryState']
        self.assertNotIn('regionSelection', state)
        self.assertNotIn('usersSearchTerm', state)
        self.assertEqual(state['mapBounds'], BOUNDS)
        self.assertTrue(stats.limit_reached)

    async def test_radius_can_include_neighbors_and_strict_mode_adds_region(self):
        corner = search.bounds_for_radius(41.75, -88.16, 10)
        data = upstream([card(1), card(2, addressCity='Aurora'),
                         card(3, corner['north'], corner['east'])])
        for strict in (False, True):
            with patch.object(search, 'resolve_search_location', AsyncMock(return_value=LOCATION)), \
                 patch.object(search, '_fetch_search_payload', AsyncMock(return_value=(data, 1))) as fetch:
                listings, location, stats = await search.discover_zillow_listings(
                    pool=None, location_query='Naperville, IL', radius_miles=10, strict_location=strict)
            self.assertEqual([x.zpid for x in listings], ['1'] if strict else ['1', '2'])
            self.assertEqual(stats.discarded_out_of_area, 1)
            self.assertEqual('regionSelection' in fetch.call_args.kwargs['payload']['searchQueryState'], strict)

    async def test_pagination_handles_zillows_search_list_count_and_deduplicates(self):
        requests = []
        async def fetch(**kwargs):
            state = kwargs['payload']['searchQueryState']; requests.append(state)
            page = state.get('pagination', {}).get('currentPage', 1)
            return upstream([card(1), card(2)] if page == 1 else [card(2), card(3)], 3, 2), 1
        with patch.object(search, '_fetch_search_payload', fetch):
            listings, _, stats = await search.discover_zillow_listings(
                pool=None, bbox=BOUNDS, tile_if_needed=False, status='sold', sold_in_last='12m',
                year_built_min=2000, year_built_max=2020)
        self.assertEqual([x.zpid for x in listings], ['1', '2', '3'])
        self.assertEqual(stats.search_requests, 2)
        self.assertFalse(stats.coverage_limited)
        self.assertTrue(all(s['filterState']['doz'] == {'value': '12m'} for s in requests))
        self.assertTrue(all(s['filterState']['built'] == {'min': 2000, 'max': 2020} for s in requests))

    async def test_tiling_budget_and_concurrency_are_bounded(self):
        active = peak = 0
        async def fetch(**kwargs):
            nonlocal active, peak
            active += 1; peak = max(active, peak)
            await asyncio.sleep(0)
            active -= 1
            return upstream([card(1)], 500, 20), 1
        with patch.object(search, '_fetch_search_payload', fetch):
            listings, _, stats = await search.discover_zillow_listings(
                pool=None, bbox=BOUNDS, tile_depth=3, search_concurrency=2, max_search_requests=5)
        self.assertEqual(stats.search_requests, 5)
        self.assertLessEqual(peak, 2)
        self.assertTrue(stats.tiled)
        self.assertTrue(stats.coverage_limited)
        self.assertEqual(len(listings), 1)

    async def test_duplicate_pages_stop_without_filling_budget(self):
        with patch.object(search, '_fetch_search_payload', AsyncMock(return_value=(upstream([card(1)], 500, 20), 1))):
            _, _, stats = await search.discover_zillow_listings(pool=None, bbox=BOUNDS, tile_if_needed=False)
        self.assertEqual(stats.search_requests, 2)
        self.assertTrue(stats.coverage_limited)

    async def test_geocoding_failure_is_controlled(self):
        transport = httpx.MockTransport(lambda _: httpx.Response(503))
        real_client = httpx.AsyncClient
        with patch.object(search.httpx, 'AsyncClient', lambda **kw: real_client(transport=transport, **kw)):
            with self.assertRaises(search.SearchError):
                await search.resolve_search_location('Naperville, IL')


class SearchRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.app = FastAPI(); self.app.include_router(routes.router)
        self.search_pool = SimpleNamespace(stats={'size': 4, 'live': 4})
        self.detail_pool = SimpleNamespace(stats={'size': 24, 'live': 24})
        self.app.state.search_pool = self.search_pool
        self.app.state.pool = self.detail_pool
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url='http://test')
        self.addAsyncCleanup(self.client.aclose)
        self.cache_patch = patch.object(routes, '_listing_cache', TTLCache())
        self.cache_patch.start(); self.addCleanup(self.cache_patch.stop)

    async def test_invalid_http_request_is_422_before_network(self):
        with patch.object(search, '_fetch_search_payload', side_effect=AssertionError('network')):
            for body in ({}, {'bbox': BOUNDS, 'location': 'Naperville'}, {'bbox': BOUNDS, 'sold_in_last': '30'}):
                response = await self.client.post('/v2/search/listings', json=body)
                self.assertEqual(response.status_code, 422)

    async def test_previews_work_when_detail_service_is_unavailable(self):
        self.detail_pool.stats['live'] = 0
        with patch.object(search, '_fetch_search_payload', AsyncMock(return_value=(upstream([card(1)]), 1))), \
             patch.object(routes, 'scrape_urls_via_px', side_effect=AssertionError('detail fetch')):
            response = await self.client.post('/v2/search/listings', json={'bbox': BOUNDS})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body['summary']['area']['mode'], 'bbox')
        self.assertEqual(body['summary']['discovered'], 1)
        self.assertIsNone(body['summary']['location'])
        health = await self.client.get('/health')
        self.assertEqual(health.status_code, 503)
        self.assertEqual(health.json()['status'], 'search-only')

    async def test_default_search_ignores_detail_cache_and_needs_no_detail_pool(self):
        del self.app.state.pool
        routes._listing_cache.set('1', PropertyListing(zpid='1', price=999, status='success', latitude=50), 60)
        with patch.object(search, '_fetch_search_payload', AsyncMock(return_value=(upstream([card(1, price='$123')]), 1))), \
             patch.object(routes, 'scrape_urls_via_px', side_effect=AssertionError('detail fetch')), \
             patch.object(routes._listing_cache, 'get', side_effect=AssertionError('detail cache read')), \
             patch.object(routes._listing_cache, 'set', side_effect=AssertionError('detail cache write')):
            response = await self.client.post('/v2/search/listings', json={'bbox': BOUNDS})
        self.assertEqual(response.status_code, 200)
        result = response.json()['results'][0]
        self.assertEqual(result['price'], 123)
        self.assertEqual(result['latitude'], 41.75)
        self.assertNotIn('photos', result)
        self.assertNotIn('search_preview', result)
        self.assertNotIn('pool', response.json()['summary'])
        self.assertNotIn('include_details', response.json()['summary'])
        self.assertEqual(routes._listing_cache.get('1').price, 999)

    async def test_removed_detail_options_return_422_before_network(self):
        for key, value in (('include_details', True), ('include_details', False),
                           ('cache_seconds', 0), ('concurrency', 60)):
            with self.subTest(key=key, value=value), \
                 patch.object(search, '_fetch_search_payload', side_effect=AssertionError('network')):
                response = await self.client.post('/v2/search/listings', json={'bbox': BOUNDS, key: value})
            self.assertEqual(response.status_code, 422)

    async def test_upstream_failure_is_502(self):
        with patch.object(search, '_fetch_search_payload', side_effect=search.SearchError('Zillow search returned HTTP 403')):
            response = await self.client.post('/v2/search/listings', json={'bbox': BOUNDS})
        self.assertEqual(response.status_code, 502)

    async def test_openapi_exposes_both_modes_and_disallows_unknown_fields(self):
        response = await self.client.get('/openapi.json')
        schema = response.json()['components']['schemas']['SearchListingsRequest']
        self.assertFalse(schema['additionalProperties'])
        self.assertTrue({'include_details', 'concurrency', 'cache_seconds'}.isdisjoint(schema['properties']))
        self.assertTrue({'bbox', 'location', 'new_construction_only', 'sold_in_last', 'year_built_min', 'year_built_max'} <= schema['properties'].keys())


class SearchSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_search_validation_uses_search_and_rejects_challenge(self):
        seen = []
        for response, valid in ((httpx.Response(200, json=upstream([])), True),
                                (httpx.Response(403), False), (httpx.Response(200, json={}), False)):
            def handler(request):
                seen.append(request.url.path)
                return response
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                slot = SimpleNamespace(client=client, last_success_at=None)
                pool = object.__new__(search.SearchCookiePool)
                if valid:
                    await pool._validate_slot(slot)
                    self.assertIsNotNone(slot.last_success_at)
                else:
                    with self.assertRaises(SessionValidationError):
                        await pool._validate_slot(slot)
        self.assertEqual(set(seen), {'/async-create-search-page-state'})


if __name__ == '__main__':
    unittest.main()
