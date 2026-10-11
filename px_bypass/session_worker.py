"""Run browser setup in an owned process group with a hard deadline."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
import signal
import sys

logger = logging.getLogger("px_bypass.session_worker")


class IsolatedMinter:
    """A stuck browser cannot keep the API's recovery lock indefinitely."""

    def __init__(self, *, headless: bool = True, timeout_s: float = 60.0):
        self._headless = headless
        self._timeout_s = timeout_s
        self._sem = asyncio.Semaphore(max(1, int(os.getenv("MINT_CONCURRENCY", "2"))))
        self._workers: set[asyncio.subprocess.Process] = set()
        self._closing = False

    async def _start_worker(self) -> asyncio.subprocess.Process:
        return await asyncio.create_subprocess_exec(
            sys.executable, "-m", "px_bypass.session_worker",
            cwd=Path(__file__).resolve().parent.parent,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=(os.name == "posix"),
        )

    @staticmethod
    def _kill(worker: asyncio.subprocess.Process) -> None:
        # Only this worker's owned group, including its Chrome children.
        try:
            if os.name == "posix":
                os.killpg(worker.pid, signal.SIGKILL)
            elif worker.returncode is None:
                worker.kill()
        except ProcessLookupError:
            pass

    async def mint(self, *, session_id: str) -> dict:
        async with self._sem:
            if self._closing:
                raise RuntimeError("Session minter is shutting down")
            worker = await self._start_worker()
            self._workers.add(worker)
            try:
                request = json.dumps({"session_id": session_id, "headless": self._headless,
                                      "timeout_s": self._timeout_s}).encode()
                stdout, _ = await asyncio.wait_for(worker.communicate(request), self._timeout_s)
                if worker.returncode:
                    # Child output can contain credentials; never include it in errors.
                    raise RuntimeError("Browser session worker failed")
                cookies = json.loads(stdout)
                if not isinstance(cookies, dict) or not all(
                    isinstance(k, str) and isinstance(v, str) for k, v in cookies.items()
                ):
                    raise RuntimeError("Browser session worker returned invalid data")
                return cookies
            except TimeoutError:
                logger.warning("browser session exceeded %.1fs; terminating its worker", self._timeout_s)
                raise
            finally:
                self._kill(worker)
                await asyncio.wait_for(worker.wait(), 5)
                self._workers.discard(worker)

    async def close(self) -> None:
        self._closing = True
        workers = list(self._workers)
        for worker in workers:
            self._kill(worker)
        await asyncio.gather(*[asyncio.wait_for(worker.wait(), 5) for worker in workers])


async def _main() -> None:
    from .patchright_minter import PatchrightMinter

    request = json.loads(sys.stdin.buffer.readline())
    minter = await PatchrightMinter.create(headless=request["headless"], timeout_s=request["timeout_s"])
    try:
        cookies = await minter.mint(session_id=request["session_id"])
    finally:
        await minter.close()
    # Private pipe to the parent; never write this payload to application logs.
    sys.stdout.write(json.dumps(cookies))


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except Exception as exc:
        sys.stderr.write(type(exc).__name__ + "\n")
        sys.exit(1)
