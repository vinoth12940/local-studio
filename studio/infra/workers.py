"""Opt-in bounded worker pool for slow search rendering, separate from FastAPI's quick routes."""
import asyncio
import functools
import weakref
from concurrent.futures import ThreadPoolExecutor

from starlette.concurrency import run_in_threadpool

from studio.storage.flags import flag

_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix='ls-render')
_SLOTS = weakref.WeakKeyDictionary()


def isolated_io(fn):
    @functools.wraps(fn)
    async def call(*args, **kwargs):
        if not flag('performance.isolated_io'):
            return await run_in_threadpool(fn, *args, **kwargs)
        loop = asyncio.get_running_loop()
        slots = _SLOTS.setdefault(loop, asyncio.Semaphore(32))
        async with slots:
            return await loop.run_in_executor(_POOL, functools.partial(fn, *args, **kwargs))
    return call
