import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
from fastapi import FastAPI
from starlette.requests import Request

from px_bypass import routes, scraper
from px_bypass.cookie_pool import CookiePool, CookieSlot, PoolUnavailableError
from px_bypass.patchright_minter import _solve_in_context
from px_bypass.proxy import EvomiConfig
from px_bypass.session_worker import IsolatedMinter

URL = 'https://www.zillow.com/homedetails/51816594_zpid/'
CONFIG = EvomiConfig('example.invalid', 1001, 'user', 'password', 'https')


class RetryBudgetTests(unittest.IsolatedAsyncioTestCase):
    async def test_malformed_and_error_responses_stop_at_budget(self):
        cases = [b'not json', b'[]', b'{"errors":[{"message":"test"}]}',
                 b'{"data":{"property":{"zpid":1}}}']
        for payload in cases:
            with self.subTest(payload=payload):
                async with httpx.AsyncClient() as client:
                    attempt = AsyncMock(return_value=(200, payload))
                    with patch.object(scraper, '_attempt', attempt):
                        row, _, _ = await asyncio.wait_for(scraper._fetch_one(
                            client, client, URL, {}, json.loads(scraper.QUERY_TEMPLATE_PATH.read_text()),
                            asyncio.Semaphore(1), scraper._State(), max_retries=3, skip_direct=True,
                        ), 1)
                self.assertEqual(attempt.await_count, 3)
                self.assertEqual(row.attempts, 3)
                self.assertEqual(row.status, 'error')
                self.assertEqual(row.zpid, '51816594')

    async def test_no_available_transport_finishes_without_network(self):
        async with httpx.AsyncClient() as client:
            attempt = AsyncMock()
            with patch.object(scraper, '_attempt', attempt):
                row, _, _ = await asyncio.wait_for(scraper._fetch_one(
                    client, None, URL, {}, json.loads(scraper.QUERY_TEMPLATE_PATH.read_text()),
                    asyncio.Semaphore(1), scraper._State(), skip_direct=True,
                ), 1)
        self.assertEqual(row.status, 'error')
        self.assertEqual(row.attempts, 0)
        attempt.assert_not_awaited()


class SessionAgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(403)))
        self.pool = CookiePool(AsyncMock(), 1, evomi_cfg=CONFIG, persist_ttl_s=60,
                               persist_path=Path(self.directory.name)/'slots.json', session_wait_s=0.01)
        self.slot = CookieSlot(0, {}, 'abcdefgh', self.client, asyncio.Semaphore(1),
                               time.time(), validated=True)
        self.pool._slots[0] = self.slot
        self.addAsyncCleanup(self.pool.aclose)

    async def test_expired_slot_is_unhealthy_and_cannot_be_leased(self):
        self.slot.minted_at -= 61
        app = FastAPI(); app.state.pool = self.pool
        response = await routes.health(Request({'type': 'http', 'app': app}))
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.pool.stats['live'], 0)
        with patch.object(self.pool, '_schedule_refresh') as refresh:
            with self.assertRaises(PoolUnavailableError):
                await self.pool.acquire()
        refresh.assert_called_once_with(0)

    async def test_block_preserves_actual_attempts_and_upstream_reason(self):
        with patch.object(self.pool, '_schedule_refresh'):
            row, _, _ = await scraper._fetch_one(
                self.client, None, URL, {}, json.loads(scraper.QUERY_TEMPLATE_PATH.read_text()),
                asyncio.Semaphore(1), scraper._State(), max_retries=3, skip_direct=True, pool=self.pool,
            )
        self.assertEqual(row.status, 'blocked')
        self.assertEqual(row.attempts, 1)
        self.assertIn('403', row.error_msg)
        self.assertEqual(self.pool.stats['live'], 0)

    async def test_one_attempt_block_does_not_wait_for_replacement(self):
        self.pool._session_wait_s = 60
        with patch.object(self.pool, '_schedule_refresh'):
            row, _, _ = await asyncio.wait_for(scraper._fetch_one(
                self.client, None, URL, {}, json.loads(scraper.QUERY_TEMPLATE_PATH.read_text()),
                asyncio.Semaphore(1), scraper._State(), max_retries=1, skip_direct=True, pool=self.pool,
            ), .5)
        self.assertEqual(row.status, 'blocked')
        self.assertEqual(row.attempts, 1)


class HttpSessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.mint = AsyncMock(side_effect=AssertionError('browser should not run'))
        self.pool = CookiePool(self.mint, 1, evomi_cfg=CONFIG,
                               persist_path=Path(self.directory.name)/'slots.json')
        self.addAsyncCleanup(self.pool.aclose)

    async def test_http_session_uses_server_cookie_jar_without_browser_risk_cookies(self):
        requests = []
        def respond(request):
            requests.append(request)
            return httpx.Response(200, json={'data': {'property': {'zpid': 51816594}}},
                                  headers={'set-cookie': 'zguid=test; Path=/; Secure'})
        client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        with patch.object(self.pool, '_build_client', return_value=client):
            slot = await self.pool._mint(0)
        self.mint.assert_not_awaited()
        self.assertEqual(slot.cookies, {})
        self.assertEqual(self.pool.stats['live'], 1)
        row, _, _ = await scraper._fetch_one(
            client, None, URL, {}, json.loads(scraper.QUERY_TEMPLATE_PATH.read_text()),
            asyncio.Semaphore(1), scraper._State(), skip_direct=True, pool=self.pool,
        )
        self.assertEqual(row.status, 'success')
        self.assertIn('zguid=test', requests[-1].headers['cookie'])
        self.assertNotIn('_px3', requests[-1].headers['cookie'])

    async def test_clean_sessions_persist_but_risk_cookie_sessions_are_not_imported(self):
        client = httpx.AsyncClient(transport=httpx.MockTransport(lambda req:
            httpx.Response(200, json={'data': {'property': {'zpid': 51816594}}})))
        self.addAsyncCleanup(client.aclose)
        self.pool._persist_path.write_text(json.dumps({'slots': [
            {'idx':0,'session_id':'abcdefgh','minted_at':time.time(),'cookies':{'_px3':'rejected'}}]}))
        with patch.object(self.pool, '_build_client', return_value=client):
            self.assertEqual(self.pool._rehydrate_from_disk(), 0)
            await self.pool._mint(0)
            self.pool._persist_to_disk()
            self.pool._slots = [None]
            self.assertEqual(self.pool._rehydrate_from_disk(), 1)
        self.assertFalse(self.pool._slots[0].validated)
        self.assertEqual(self.pool.stats['live'], 0)
        self.assertEqual(self.pool._slots[0].cookies, {})


class BrowserDeadlineTests(unittest.IsolatedAsyncioTestCase):
    async def test_navigation_deadline_closes_page(self):
        page = MagicMock()
        page.goto = AsyncMock(side_effect=lambda *a, **kw: None)
        async def hung_navigation(*args, **kwargs):
            await asyncio.Event().wait()
        page.goto.side_effect = hung_navigation
        page.close = AsyncMock()
        context = MagicMock(new_page=AsyncMock(return_value=page))
        with self.assertRaises(TimeoutError):
            await asyncio.wait_for(_solve_in_context(context, .01), .5)
        page.close.assert_awaited_once()

    async def start(self, code):
        return await asyncio.create_subprocess_exec(
            sys.executable, '-c', code, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            start_new_session=(os.name == 'posix'),
        )

    async def test_frozen_worker_is_reaped_and_next_mint_can_run(self):
        minter = IsolatedMinter(timeout_s=.05)
        self.addAsyncCleanup(minter.close)
        hung = await self.start('import time; time.sleep(60)')
        with patch.object(minter, '_start_worker', AsyncMock(return_value=hung)):
            with self.assertRaises(TimeoutError):
                await asyncio.wait_for(minter.mint(session_id='abcdefgh'), 1)
        self.assertIsNotNone(hung.returncode)
        self.assertEqual(minter._workers, set())
        minter._timeout_s = 1
        healthy = await self.start('print(\'{"_px3":"test"}\')')
        with patch.object(minter, '_start_worker', AsyncMock(return_value=healthy)):
            self.assertEqual(await minter.mint(session_id='abcdefgh'), {'_px3': 'test'})

    async def test_cancelled_mint_reaps_worker(self):
        minter = IsolatedMinter(timeout_s=60)
        self.addAsyncCleanup(minter.close)
        hung = await self.start('import time; time.sleep(60)')
        with patch.object(minter, '_start_worker', AsyncMock(return_value=hung)):
            task = asyncio.create_task(minter.mint(session_id='abcdefgh'))
            await asyncio.sleep(.01)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertIsNotNone(hung.returncode)
        self.assertEqual(minter._workers, set())
