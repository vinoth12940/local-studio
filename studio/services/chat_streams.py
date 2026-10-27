"""Bounded, process-local replay of opt-in chat streams, independent of any HTTP subscriber."""
import asyncio
import time
import uuid
from collections import deque

from fastapi import HTTPException

from studio.infra.sse import _sse
from studio.security.redact import redact
from studio.storage.flags import flag

MAX_STREAMS = 8
MAX_EVENTS = 4096
MAX_BYTES = 2 * 2**20
RETENTION_SECONDS = 300
_STREAMS = {}


class ChatStream:
    def __init__(self, source):
        self.id = uuid.uuid4().hex
        self.events = deque()
        self.bytes = 0
        self.last = 0
        self.done = False
        self.finished_at = None
        self.changed = asyncio.Condition()
        self.source = source
        self.task = asyncio.create_task(self.produce())

    async def append(self, frame):
        if isinstance(frame, bytes):
            frame = frame.decode('utf-8', 'replace')
        async with self.changed:
            self.last += 1
            frame = f'id: {self.last}\n' + frame
            size = len(frame.encode())
            self.events.append((self.last, frame, size))
            self.bytes += size
            while self.events and (len(self.events) > MAX_EVENTS or self.bytes > MAX_BYTES):
                self.bytes -= self.events.popleft()[2]
            self.changed.notify_all()

    async def produce(self):
        terminal_sent = False
        try:
            async for frame in self.source:
                if isinstance(frame, bytes):
                    frame = frame.decode('utf-8', 'replace')
                await self.append(frame)
                terminal_sent |= any(line.startswith('data:') and line[5:].strip() == '[DONE]'
                                     for line in frame.splitlines())
        except asyncio.CancelledError:
            await self.append(_sse({'error': 'Stopped.'}))
            await self.append('data: [DONE]\n\n')
        except Exception as exc:
            message = redact(str(exc)) if flag('security.redact') else str(exc)
            await self.append(_sse({'error': message}))
            await self.append('data: [DONE]\n\n')
        else:
            # Early error returns in relay() end normally without a terminal frame.
            # Replay clients need that frame to distinguish completion from a lost connection.
            if not terminal_sent:
                await self.append('data: [DONE]\n\n')
        finally:
            try:
                await self.source.aclose()
            finally:
                async with self.changed:
                    self.done = True
                    self.finished_at = time.monotonic()
                    self.changed.notify_all()

    def validate(self, after):
        if after < 0 or after > self.last:
            raise HTTPException(400, 'Invalid stream event cursor')
        first = self.events[0][0] if self.events else self.last + 1
        if after < first - 1:
            raise HTTPException(410, 'This stream replay window expired. The request was not restarted.')

    async def subscribe(self, after=0):
        while True:
            heartbeat = False
            async with self.changed:
                self.validate(after)
                pending = [(i, frame) for i, frame, _ in self.events if i > after]
                done = self.done
                if not pending and not done:
                    try:
                        await asyncio.wait_for(self.changed.wait(), 15)
                    except asyncio.TimeoutError:
                        heartbeat = True
            for after, frame in pending:
                yield frame
            if done:
                return
            if not pending and heartbeat:
                yield ': keepalive\n\n'

    def stop(self):
        if not self.done and not self.task.cancelling():
            self.task.cancel()


def new_stream(source):
    now = time.monotonic()
    for sid, stream in list(_STREAMS.items()):
        if stream.done and now - stream.finished_at > RETENTION_SECONDS:
            _STREAMS.pop(sid, None)
    if len(_STREAMS) >= MAX_STREAMS:
        completed = [s for s in _STREAMS.values() if s.done]
        if not completed:
            raise HTTPException(503, 'Too many active chat streams', headers={'Retry-After': '5'})
        _STREAMS.pop(min(completed, key=lambda s: s.finished_at).id)
    stream = ChatStream(source)
    _STREAMS[stream.id] = stream
    return stream


def get_stream(sid):
    if not flag('streams.resume'):
        raise HTTPException(404, 'Stream resumption is disabled')
    stream = _STREAMS.get(sid)
    if stream is None or (stream.done and time.monotonic() - stream.finished_at > RETENTION_SECONDS):
        _STREAMS.pop(sid, None)
        raise HTTPException(404, 'This stream is no longer available; it was not restarted')
    return stream


class DetachedRequest:
    """The producer outlives HTTP disconnection. Explicit Stop cancels its task instead."""
    async def is_disconnected(self):
        return False
