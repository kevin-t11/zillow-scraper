import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI, HTTPException
from starlette.requests import Request

from px_bypass import routes, scraper
from px_bypass.cookie_pool import CookiePool, PoolUnavailableError, SessionValidationError
from px_bypass.proxy import EvomiConfig, build_proxy_url, split_proxy_url
from px_bypass.patchright_minter import _proxy_arg

CONFIG = EvomiConfig('example.invalid', 1001, 'user', 'password', 'https')


class ProxyTests(unittest.TestCase):
    def test_https_preserved_by_http_and_browser_clients(self):
        url = build_proxy_url(CONFIG, session='test')
        self.assertTrue(url.startswith('https://'))
        self.assertEqual(split_proxy_url(url)['server'], 'https://example.invalid:1001')
        with patch.object(EvomiConfig, 'from_env', return_value=CONFIG):
            self.assertEqual(_proxy_arg('test')['server'], 'https://example.invalid:1001')
        self.assertIn('_session-test_lifetime-30', split_proxy_url(url)['password'])

    def test_default_configuration_keeps_http_compatibility(self):
        cfg = EvomiConfig('example.invalid', 1000, 'user', 'password')
        self.assertTrue(build_proxy_url(cfg).startswith('http://'))


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.pool = CookiePool(AsyncMock(return_value={'_px3': 'test'}), 2,
                               evomi_cfg=CONFIG, persist_path=Path(self.directory.name)/'slots.json',
                               session_wait_s=0.01, retry_delay_s=0.05, prefer_http_sessions=False)
        self.addAsyncCleanup(self.pool.aclose)

    def client(self, response):
        return httpx.AsyncClient(transport=httpx.MockTransport(lambda req: response))

    async def test_rejected_cookie_never_published_and_client_closed(self):
        client = self.client(httpx.Response(403))
        with patch.object(self.pool, '_build_client', return_value=client):
            with self.assertRaises(SessionValidationError):
                await self.pool._mint(0)
        self.assertEqual(self.pool.stats['live'], 0)
        self.assertEqual(self.pool.stats['validation_failures'], 1)
        self.assertTrue(client.is_closed)
        self.assertIsNone(self.pool._slots[0])

    async def test_valid_property_published_after_validation(self):
        client = self.client(httpx.Response(200, json={'data': {'property': {'zpid': 51816594}}}))
        with patch.object(self.pool, '_build_client', return_value=client):
            slot = await self.pool._mint(0)
        self.assertTrue(slot.validated)
        self.assertEqual(self.pool.stats['live'], 1)
        self.assertIs(await self.pool.acquire(), slot)

    async def test_malformed_or_wrong_property_cannot_become_live(self):
        for payload in [[], {'data': []}, {'data': {'property': 'bad'}},
                        {'data': {'property': {'zpid': 1}}}]:
            client = self.client(httpx.Response(200, json=payload))
            with patch.object(self.pool, '_build_client', return_value=client):
                with self.assertRaises(SessionValidationError):
                    await self.pool._mint(0)
            self.assertTrue(client.is_closed)
        self.assertEqual(self.pool.stats['live'], 0)

    async def test_empty_pool_has_bounded_wait(self):
        with self.assertRaises(PoolUnavailableError):
            await asyncio.wait_for(self.pool.acquire(), 0.2)

    async def test_startup_outage_probes_once_and_keeps_api_available(self):
        mint = AsyncMock(side_effect=RuntimeError('test'))
        with patch.object(EvomiConfig, 'from_env', return_value=CONFIG):
            pool = await CookiePool.create(mint, 24, persist_path=Path(self.directory.name)/'startup.json',
                                            prefer_http_sessions=False)
        try:
            self.assertEqual(mint.await_count, 1)
            self.assertEqual(pool.stats['live'], 0)
            self.assertEqual(pool.stats['refreshing'], 1)
        finally:
            await pool.aclose()
        self.assertFalse(pool._background_tasks)

    async def test_background_retry_respects_cooldown_and_shutdown_cancels(self):
        mint = AsyncMock(side_effect=RuntimeError('test'))
        self.pool._mint_fn = mint
        self.pool._schedule_refresh(0)
        await asyncio.sleep(0.02)
        self.assertEqual(mint.await_count, 1)
        await self.pool.aclose()
        self.assertFalse(self.pool._background_tasks)
        self.assertEqual(self.pool.stats['refreshing'], 0)

    async def test_no_session_batch_finishes_and_preserves_all_inputs(self):
        urls = [f'https://www.zillow.com/homedetails/{i}_zpid/' for i in range(100)]
        results, stats = await asyncio.wait_for(
            scraper.scrape_urls_via_px(urls, pool=self.pool, concurrency=2, max_retries=1), 1)
        self.assertEqual(len(results), 100)
        self.assertTrue(all(row.status == 'blocked' for row in results))
        self.assertEqual(stats.total, 100)
        self.assertEqual({row.zpid for row in results}, {str(i) for i in range(100)})

    async def test_health_and_endpoint_report_unavailable(self):
        app = FastAPI()
        app.state.pool = self.pool
        req = Request({'type': 'http', 'app': app})
        response = await routes.health(req)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(json.loads(response.body)['status'], 'no-cookies')
        with self.assertRaises(HTTPException) as exc:
            routes._pool(req)
        self.assertEqual(exc.exception.status_code, 503)
        self.assertEqual(exc.exception.headers['Retry-After'], '60')
