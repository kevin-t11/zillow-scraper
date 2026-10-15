import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from px_bypass.cookie_pool import CookiePool, CookieSlot, SessionValidationError
from px_bypass.proxy import EvomiConfig


class PoolReliabilityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.pool = CookiePool(AsyncMock(), 3,
            evomi_cfg=EvomiConfig('proxy.invalid', 1000, 'user', 'password'),
            persist_path=Path(directory.name)/'slots.json', persist_ttl_s=90,
            retry_delay_s=100, session_wait_s=.1)
        self.addAsyncCleanup(self.pool.aclose)

    def client(self, status=200):
        client = httpx.AsyncClient(transport=httpx.MockTransport(lambda req:
            httpx.Response(status, json={'data': {'property': {'zpid': 51816594}}})))
        self.addAsyncCleanup(client.aclose)
        return client

    def live_slot(self, idx=0):
        slot = CookieSlot(idx, {}, f'session{idx}', self.client(), asyncio.Semaphore(1),
                          time.time(), validated=True)
        self.pool._slots[idx] = slot
        return slot

    async def test_renewal_starts_before_expiry_without_interrupting_acquisition(self):
        slot = self.live_slot()
        slot.minted_at -= 65
        with patch.object(self.pool, '_schedule_refresh') as refresh:
            self.pool._expire_slots()
            self.assertIs(await self.pool.acquire(), slot)
        refresh.assert_called_with(0)
        self.assertTrue(self.pool.usable(slot))
        self.assertFalse(slot.client.is_closed)

    async def test_failed_candidate_preserves_live_session_and_closes_candidate(self):
        slot = self.live_slot()
        candidate = self.client(403)
        with patch.object(self.pool, '_build_client', return_value=candidate):
            with self.assertRaises(SessionValidationError):
                await self.pool._mint(0)
        self.assertIs(await self.pool.acquire(), slot)
        self.assertTrue(candidate.is_closed)
        self.assertFalse(slot.client.is_closed)
        self.assertEqual(self.pool.stats['live'], 1)

    async def test_cancelled_validation_preserves_live_session_and_closes_candidate(self):
        slot = self.live_slot()
        candidate = self.client()
        started = asyncio.Event()
        async def validate(_):
            started.set()
            await asyncio.Event().wait()
        with patch.object(self.pool, '_build_client', return_value=candidate), patch.object(
            self.pool, '_validate_slot', validate):
            task = asyncio.create_task(self.pool._mint(0))
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertIs(await self.pool.acquire(), slot)
        self.assertTrue(candidate.is_closed)

    async def test_successful_replacement_is_atomic_and_late_old_rejection_is_ignored(self):
        old = self.live_slot()
        candidate = self.client()
        with patch.object(self.pool, '_build_client', return_value=candidate):
            new = await self.pool._mint(0)
        self.assertIs(await self.pool.acquire(), new)
        self.assertEqual(self.pool.stats['proactive_renewals'], 1)
        self.assertFalse(old.client.is_closed)  # in-flight leases can finish
        with patch.object(self.pool, '_schedule_refresh') as refresh:
            self.pool.mark_dead(old)
        refresh.assert_not_called()
        self.assertTrue(self.pool.usable(new))

    async def test_one_bad_candidate_does_not_delay_other_slots(self):
        self.live_slot()
        failed = asyncio.Event()
        recovered = asyncio.Event()
        attempts = []
        async def mint(idx):
            attempts.append(idx)
            if idx == 1:
                failed.set()
                raise SessionValidationError('Validation listing returned HTTP 403')
            self.live_slot(idx)
            recovered.set()
        with patch.object(self.pool, '_mint', mint):
            self.pool._schedule_refresh(1)
            await failed.wait()
            self.pool._schedule_refresh(2)
            await asyncio.wait_for(recovered.wait(), .5)
            self.assertEqual(attempts, [1, 2])
            self.assertEqual(self.pool._retry_after, 0)
            await self.pool.aclose()

    async def test_complete_outage_uses_one_shared_probe_budget(self):
        failed = asyncio.Event()
        attempts = []
        async def mint(idx):
            attempts.append(idx)
            failed.set()
            raise SessionValidationError('Validation listing returned HTTP 403')
        with patch.object(self.pool, '_mint', mint):
            for idx in range(3):
                self.pool._schedule_refresh(idx)
            await failed.wait()
            await asyncio.sleep(.02)
            self.assertEqual(len(attempts), 1)
            self.assertEqual(self.pool.stats['live'], 0)
            await self.pool.aclose()

    async def test_startup_serves_after_one_restored_session_and_validates_rest_in_background(self):
        slots = [CookieSlot(i, {}, f'session{i}', self.client(), asyncio.Semaphore(1),
                            time.time()) for i in range(3)]
        gate = asyncio.Event()
        done = asyncio.Event()
        calls = []
        def restore(pool):
            pool._slots = slots
            return 3
        async def validate(pool, slot):
            calls.append(slot.idx)
            if slot.idx != 0:
                await gate.wait()
            if len(calls) == 3:
                done.set()
        with patch.object(CookiePool, '_rehydrate_from_disk', restore), patch.object(
            CookiePool, '_validate_slot', validate), patch.object(
            EvomiConfig, 'from_env', return_value=self.pool._evomi):
            pool = await asyncio.wait_for(CookiePool.create(AsyncMock(), 3,
                persist_path=self.pool._persist_path), .5)
            self.addAsyncCleanup(pool.aclose)
            self.assertEqual(pool.stats['live'], 1)
            self.assertFalse(slots[1].validated)
            gate.set()
            await asyncio.wait_for(done.wait(), .5)
            self.assertEqual(pool.stats['live'], 3)
            await pool.aclose()
