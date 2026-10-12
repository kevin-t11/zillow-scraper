import asyncio
import json
import time
import unittest
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from fastapi import FastAPI
from starlette.requests import Request

from app.models import PropertyListing
from px_bypass import resolver, routes, scraper
from px_bypass.cache import TTLCache, normalize_address
from px_bypass.cookie_pool import CookiePool, CookieSlot
from px_bypass.proxy import EvomiConfig


def request(pool=None):
    app = FastAPI()
    if pool is not None:
        app.state.pool = pool
    return Request({'type': 'http', 'app': app})


def url(zpid):
    return f'https://www.zillow.com/homedetails/{zpid}_zpid/'


class AddressPipelineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.pool = SimpleNamespace(stats={'size': 24, 'live': 24})
        self.patches = [
            patch.object(routes, '_listing_cache', TTLCache()),
            patch.object(routes, '_resolve_cache', TTLCache()),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    async def test_fetch_starts_before_last_resolution_finishes(self):
        fetched = asyncio.Event()

        async def resolve(addresses, **kwargs):
            yield 0, (addresses[0], '1', url('1'))
            await asyncio.wait_for(fetched.wait(), 1)
            yield 1, (addresses[1], '2', url('2'))

        async def fetch(*args):
            fetched.set()
            zpid = scraper.parse_listing_url(args[2])[1]
            return PropertyListing(zpid=zpid, url=args[2], status='success'), 1, 'proxy'

        with patch.object(routes, 'iter_resolved_addresses', resolve), patch.object(scraper, '_fetch_one', fetch):
            response = await routes.scrape_addresses_v2(
                routes.ScrapeAddressesRequest(addresses=['first', 'last'], cache_seconds=0),
                request(self.pool),
            )
        self.assertEqual([r['input_address'] for r in response.results], ['first', 'last'])
        self.assertEqual(response.summary['success'], 2)
        self.assertEqual(response.summary['listing_fetched'], 2)
        self.assertEqual(routes._listing_cache.size, 0)
        self.assertEqual(routes._resolve_cache.size, 0)

    async def test_aliases_deduplicate_fetch_and_do_not_mutate_cache(self):
        fetches = []

        async def resolve(addresses, **kwargs):
            for i, addr in reversed(list(enumerate(addresses))):
                yield i, (addr, '1', url('1'))

        async def fetch(*args):
            fetches.append(args[2])
            return PropertyListing(zpid='1', url=args[2], status='success'), 1, 'proxy'

        with patch.object(routes, 'iter_resolved_addresses', resolve), patch.object(scraper, '_fetch_one', fetch):
            response = await routes.scrape_addresses_v2(
                routes.ScrapeAddressesRequest(addresses=['a', 'b', 'a']), request(self.pool),
            )
        self.assertEqual(fetches, [url('1')])
        self.assertEqual([r['input_address'] for r in response.results], ['a', 'b', 'a'])
        self.assertEqual(response.summary['success'], 3)
        self.assertEqual(response.summary['resolve_fetched'], 3)
        self.assertEqual(response.summary['listing_fetched'], 1)
        self.assertEqual(routes._listing_cache.get('1').input_address, '')

    async def test_fully_cached_batch_needs_no_pool(self):
        routes._resolve_cache.set(normalize_address('a'), url('1'), 60)
        routes._listing_cache.set('1', PropertyListing(zpid='1', status='success'), 60)
        with patch.object(scraper, '_fetch_one', side_effect=AssertionError('network fetch')):
            response = await routes.scrape_addresses_v2(
                routes.ScrapeAddressesRequest(addresses=['a']), request(),
            )
        self.assertEqual(response.summary['success'], 1)
        self.assertEqual(response.summary['listing_fetched'], 0)
        self.assertEqual(response.summary['resolve_cached'], 1)

    async def test_unresolved_and_parse_errors_keep_all_input_rows(self):
        async def resolve(addresses, **kwargs):
            yield 1, (addresses[1], '2', url('2'))
            yield 0, None

        async def fetch(*args):
            return PropertyListing(url=args[2], status='parse_error'), 1, 'proxy'

        with patch.object(routes, 'iter_resolved_addresses', resolve), patch.object(scraper, '_fetch_one', fetch):
            response = await routes.scrape_addresses_v2(
                routes.ScrapeAddressesRequest(addresses=['missing', 'bad response'], cache_seconds=0), request(self.pool),
            )
        self.assertEqual([r['status'] for r in response.results], ['not_found', 'parse_error'])
        self.assertEqual(response.summary['not_found'], 1)
        self.assertEqual(response.summary['other_error'], 1)

    async def test_uncached_request_bypasses_existing_cache(self):
        routes._resolve_cache.set('a', url('99'), 60)
        routes._listing_cache.set('1', PropertyListing(zpid='1', price=999, status='success'), 60)
        calls = []

        async def resolve(addresses, **kwargs):
            calls.extend(addresses)
            yield 0, (addresses[0], '1', url('1'))

        async def fetch(*args):
            return PropertyListing(zpid='1', url=args[2], price=123, status='success'), 1, 'proxy'

        with patch.object(routes, 'iter_resolved_addresses', resolve), patch.object(scraper, '_fetch_one', fetch):
            response = await routes.scrape_addresses_v2(
                routes.ScrapeAddressesRequest(addresses=['a'], cache_seconds=0), request(self.pool),
            )
        self.assertEqual(calls, ['a'])
        self.assertEqual(response.results[0]['price'], 123)
        self.assertEqual(response.summary['resolve_cached'], 0)
        self.assertEqual(response.summary['listing_cached'], 0)
        self.assertEqual(routes._listing_cache.get('1').price, 999)

    async def test_fetch_worker_limit_and_ordinary_url_inputs(self):
        active = peak = 0

        async def fetch(*args):
            nonlocal active, peak
            active += 1
            peak = max(active, peak)
            await asyncio.sleep(0.001)
            active -= 1
            zpid = scraper.parse_listing_url(args[2])[1]
            return PropertyListing(zpid=zpid, url=args[2], status='success'), 1, 'proxy'

        with patch.object(scraper, '_fetch_one', fetch):
            results, stats = await scraper.scrape_urls_via_px(
                [url(str(i)) for i in range(15)], pool=self.pool, concurrency=3,
            )
        self.assertEqual(len(results), 15)
        self.assertEqual(stats.total, 15)
        self.assertEqual(stats.success, 15)
        self.assertLessEqual(peak, 3)

    async def test_producer_failure_cancels_inflight_fetches(self):
        started, stopped = asyncio.Event(), asyncio.Event()

        async def source():
            yield url('1')
            await started.wait()
            raise ValueError('resolver failed')

        async def fetch(*args):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        with patch.object(scraper, '_fetch_one', fetch):
            with self.assertRaisesRegex(ValueError, 'resolver failed'):
                await asyncio.wait_for(scraper.scrape_urls_via_px(
                    source(), pool=self.pool, concurrency=2,
                ), 1)
        self.assertTrue(stopped.is_set())

    async def test_worker_failure_closes_a_backpressured_producer(self):
        blocked, closed = asyncio.Event(), asyncio.Event()

        async def source():
            try:
                for i in range(10):
                    if i == 2:
                        blocked.set()
                    yield url(str(i))
            finally:
                closed.set()

        async def fetch(*args):
            await blocked.wait()
            raise RuntimeError('fetch failed')

        with patch.object(scraper, '_fetch_one', fetch):
            with self.assertRaisesRegex(RuntimeError, 'fetch failed'):
                await asyncio.wait_for(scraper.scrape_urls_via_px(
                    source(), pool=self.pool, concurrency=1,
                ), 1)
        self.assertTrue(closed.is_set())

    async def test_resolver_cancellation_closes_pending_requests(self):
        started, stopped = asyncio.Event(), asyncio.Event()

        async def resolve(client, address, **kwargs):
            if address == 'fast':
                await started.wait()
                return address, '1', url('1')
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        with patch.object(resolver, 'resolve_one', resolve):
            source = resolver.iter_resolved_addresses(['fast', 'slow'])
            await asyncio.wait_for(anext(source), 1)
            await source.aclose()
        self.assertTrue(stopped.is_set())

    async def test_unit_resolution_skips_wrong_apartment(self):
        def respond(req):
            self.assertEqual(req.url.params['resultCount'], '12')
            return httpx.Response(200, json={'results': [
                {'display': '123 Main St #8, City, CA 90240', 'metaData': {'zpid': 8}},
                {'display': '123 Main St #7, City, CA 90240', 'metaData': {'zpid': 7}},
            ]})
        from app.scraper import get_zpid_via_autocomplete
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            result = await get_zpid_via_autocomplete(
                client, '123 Main St Unit 7, City, CA 90240', prefer_full_address=True,
            )
        self.assertEqual(result[0], '7')

    async def test_search_fallback_does_not_guess_unit_identity(self):
        async def autocomplete(*args, **kwargs):
            return None
        with patch.object(resolver, 'get_zpid_via_autocomplete', autocomplete), patch.object(
            resolver, 'get_zpid_via_zillow_search', side_effect=AssertionError('unsafe unit fallback'),
        ):
            result = await resolver.resolve_one(None, '123 Main St Unit 7, City, CA 90240')
        self.assertIsNone(result)

    async def test_unit_notation_resolves_exact_unit_without_accepting_nearby_units(self):
        queries = []
        def respond(req):
            query = req.url.params['q']; queries.append(query)
            correct = '#1111N' in query
            unit = '1111N' if correct else '1110N'
            return httpx.Response(200, json={'results': [{
                'display': f'3701 S George Mason Dr #{unit} Falls Church, VA 22041',
                'metaData': {'zpid': 51853587 if correct else 51853586, 'unitNumber': unit},
            }]})
        from app.scraper import get_zpid_via_autocomplete
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            result = await get_zpid_via_autocomplete(
                client, '3701 S George Mason Dr Unit 1111N, Falls Church, VA 22041',
                prefer_full_address=True,
            )
        self.assertEqual(result[0], '51853587')
        self.assertEqual(len(queries), 1)
        self.assertIn('#1111N', queries[0])

    async def test_precise_autocomplete_is_first_and_fallback_remains(self):
        queries = []
        def respond(req):
            queries.append(req.url.params['q'])
            matches = [] if len(queries) == 1 else [
                {'display': '123 Main Street, City, CA 90240', 'metaData': {'zpid': 1}}
            ]
            return httpx.Response(200, json={'results': matches})

        from app.scraper import get_zpid_via_autocomplete
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            result = await get_zpid_via_autocomplete(
                client, '123 Main Street, City, CA 90240', prefer_full_address=True,
            )
        self.assertEqual(queries[0], '123 Main Street, City, CA 90240')
        self.assertEqual(result[0], '1')
        self.assertEqual(len(queries), 2)


class UrlEndpointTests(unittest.IsolatedAsyncioTestCase):
    setUp = AddressPipelineTests.setUp

    async def test_uncached_aliases_fetch_one_copy_per_zpid(self):
        routes._listing_cache.set('1', PropertyListing(zpid='1', price=999, status='success'), 60)
        first = url('1')
        alias = 'https://www.zillow.com/homedetails/alternate-slug/1_zpid/'
        calls = []
        async def fetch(*args):
            calls.append(args[2])
            return PropertyListing(zpid='1', url=args[2], price=123, status='success'), 1, 'proxy'
        with patch.object(scraper, '_fetch_one', fetch):
            response = await routes.scrape_urls_v2(
                routes.ScrapeUrlsRequest(urls=[first, alias, first], cache_seconds=0), request(self.pool),
            )
        self.assertEqual(calls, [first])
        self.assertEqual([r['url'] for r in response.results], [first, alias, first])
        self.assertEqual([r['price'] for r in response.results], [123, 123, 123])
        self.assertEqual(response.summary['success'], 3)
        self.assertEqual(response.summary['fetched'], 1)
        self.assertEqual(response.summary['cached'], 0)
        self.assertEqual(response.summary['deduplicated'], 2)
        self.assertEqual(routes._listing_cache.get('1').price, 999)

    async def test_mixed_cache_alias_and_invalid_rows_preserve_input_order(self):
        original = PropertyListing(zpid='1', status='success', url='original', input_address='original')
        routes._listing_cache.set('1', original, 60)
        alias = 'https://www.zillow.com/homedetails/alternate/2_zpid/'
        inputs = [url('2'), url('1'), alias, 'invalid']
        async def fetch(*args):
            if args[2] == 'invalid':
                return PropertyListing(input_address='invalid', status='error'), 0, 'fail'
            await asyncio.sleep(0.001)
            return PropertyListing(zpid='2', url=args[2], status='success'), 1, 'proxy'
        with patch.object(scraper, '_fetch_one', fetch):
            response = await routes.scrape_urls_v2(routes.ScrapeUrlsRequest(urls=inputs), request(self.pool))
        self.assertEqual([r['input_address'] for r in response.results], inputs)
        self.assertEqual([r['status'] for r in response.results], ['success', 'success', 'success', 'error'])
        self.assertEqual(response.summary['cached'], 1)
        self.assertEqual(response.summary['fetched'], 2)
        self.assertEqual(response.summary['unique_total'], 3)
        self.assertEqual(original.url, 'original')
        self.assertEqual(original.input_address, 'original')

    async def test_cached_batch_needs_no_pool_and_keeps_each_alias(self):
        routes._listing_cache.set('1', PropertyListing(zpid='1', status='success'), 60)
        inputs = [url('1'), 'https://www.zillow.com/homedetails/alternate/1_zpid/']
        response = await routes.scrape_urls_v2(routes.ScrapeUrlsRequest(urls=inputs), request())
        self.assertEqual(response.summary['cached'], 2)
        self.assertEqual(response.summary['fetched'], 0)
        self.assertEqual([r['url'] for r in response.results], inputs)

    async def test_gzip_negotiation_reduces_wire_size_and_preserves_json(self):
        from px_bypass.serve import app
        routes._listing_cache.set('1', PropertyListing(
            zpid='1', status='success', description='A large listing description. ' * 2000,
        ), 60)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url='http://test') as client:
            compressed = await client.post('/v2/scrape/urls', json={'urls': [url('1')]},
                                           headers={'accept-encoding': 'gzip'})
            plain = await client.post('/v2/scrape/urls', json={'urls': [url('1')]},
                                      headers={'accept-encoding': 'identity'})
        self.assertEqual(compressed.headers['content-encoding'], 'gzip')
        self.assertNotIn('content-encoding', plain.headers)
        self.assertEqual(compressed.json()['results'], plain.json()['results'])
        self.assertLess(int(compressed.headers['content-length']), len(compressed.content) // 10)

    async def test_invalid_url_does_not_hide_a_valid_listing(self):
        async def fetch(*args):
            if args[2].startswith('invalid'):
                return PropertyListing(input_address=args[2], status='error'), 0, 'fail'
            return PropertyListing(zpid='1', url=args[2], status='success'), 1, 'proxy'
        with patch.object(scraper, '_fetch_one', fetch):
            response = await routes.scrape_urls_v2(
                routes.ScrapeUrlsRequest(urls=['invalid/1_zpid/', url('1')], cache_seconds=0), request(self.pool),
            )
        self.assertEqual([r['status'] for r in response.results], ['error', 'success'])
        self.assertEqual(response.summary['unique_total'], 2)


class PoolSchedulingTests(unittest.IsolatedAsyncioTestCase):
    def make_pool(self):
        async def mint(**kwargs):
            return {'_px3': 'test'}
        pool = CookiePool(mint, 2, evomi_cfg=EvomiConfig('example.test', 80, 'test', 'test'))
        pool._slots = [CookieSlot(
            idx=i, cookies={}, session_id=str(i), client=None,
            sem=asyncio.Semaphore(1), minted_at=time.time(),
            validated=True,
        ) for i in range(2)]
        return pool

    async def test_reuses_available_slot_instead_of_queueing_behind_busy_one(self):
        pool = self.make_pool()
        async with pool.lease() as busy:
            async with pool.lease() as available:
                self.assertNotEqual(busy.idx, available.idx)
            async with pool.lease() as next_slot:
                self.assertEqual(next_slot.idx, available.idx)
        self.assertEqual([s.pending for s in pool._slots], [0, 0])

    async def test_existing_raw_acquire_callers_do_not_leak_reservations(self):
        pool = self.make_pool()
        for _ in range(10):
            await pool.acquire()
        self.assertEqual(sum(s.pending for s in pool._slots), 0)

    async def test_cancelled_lease_releases_reservation(self):
        pool = self.make_pool()
        started = asyncio.Event()
        async def wait():
            async with pool.lease():
                started.set()
                await asyncio.Event().wait()
        task = asyncio.create_task(wait())
        await started.wait()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(sum(s.pending for s in pool._slots), 0)

    async def test_actual_fetch_uses_and_releases_pool_lease(self):
        pool = self.make_pool()
        def respond(req):
            zpid = str(json.loads(req.url.params['variables'])['zpid'])
            return httpx.Response(200, json={'data': {'property': {'zpid': zpid}}})
        clients = [httpx.AsyncClient(transport=httpx.MockTransport(respond)) for _ in range(2)]
        for slot, client in zip(pool._slots, clients):
            slot.client = client
        try:
            rows, stats = await scraper.scrape_urls_via_px(
                [url(str(i)) for i in range(4)], pool=pool, concurrency=3, skip_direct=True,
            )
            self.assertEqual(stats.success, 4)
            self.assertEqual({r['zpid'] for r in (row.to_dict() for row in rows)}, {'0', '1', '2', '3'})
            self.assertEqual(sum(s.pending for s in pool._slots), 0)
        finally:
            await pool.aclose()


class SerializationTests(unittest.TestCase):
    def test_serialization_preserves_output_and_isolates_nested_values(self):
        listing = PropertyListing(raw_json={'large': [1, 2]}, schools=[{'name': 'A'}])
        expected = asdict(listing)
        expected.pop('raw_json')
        actual = listing.to_dict()
        self.assertEqual(actual, expected)
        actual['schools'][0]['name'] = 'B'
        self.assertEqual(listing.schools[0]['name'], 'A')
        self.assertEqual(listing.to_dict(include_raw=True), asdict(listing))

    def test_request_template_remains_exact_and_immutable(self):
        template = json.loads(scraper.QUERY_TEMPLATE_PATH.read_text())
        original = json.loads(json.dumps(template))
        a = scraper._build_request('1', 'a', template)[1]
        b = scraper._build_request('2', 'b', template)[1]
        self.assertEqual(template, original)
        self.assertEqual(a['variables']['zpid'], 1)
        self.assertEqual(b['variables']['zpid'], 2)
        self.assertEqual(a['extensions'], original['extensions'])
        self.assertEqual(b['extensions'], original['extensions'])
        self.assertNotIn('query', a)


if __name__ == '__main__':
    unittest.main()
