"""Shared test helpers."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable


async def wait_until(predicate: Callable[[], bool], timeout: float) -> None:
    """Poll ``predicate`` on the running loop until true, or fail after ``timeout``."""
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"Condition not met within {timeout}s")
        await asyncio.sleep(0.01)
