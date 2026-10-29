"""Helpers to run blocking SDK calls without letting a hung provider hang the conversation."""

import asyncio
from typing import Callable, TypeVar
from src.core.exceptions import ServiceTimeoutError
from src.core.logging import logger

T = TypeVar("T")


async def run_blocking(fn: Callable[..., T], *args, timeout: float, label: str = "", **kwargs) -> T:
    """Run a blocking call in a worker thread, bounded by `timeout` seconds.

    `asyncio.wait_for` cancels the wait, not the thread that is already running, so the caller must treat
    a timeout as "unknown outcome" and say so to the user instead of assuming the operation failed.
    """
    try:
        return await asyncio.wait_for(asyncio.to_thread(fn, *args, **kwargs), timeout=timeout)
    except asyncio.TimeoutError as e:
        logger.error(f"Timeout after {timeout:.0f}s while running {label or getattr(fn, '__name__', 'call')}.")
        raise ServiceTimeoutError(f"{label or 'operation'} timed out after {timeout:.0f}s") from e
