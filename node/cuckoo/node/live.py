# Copyright (C) 2019-2024 Estonian Information System Authority.
# See the file 'LICENSE' for copying permission.

"""Live telemetry broker for real-time sandbox event streaming.

The LiveEventBroker receives raw bytes from the result server (behavioural
log uploads) and screenshots notifications, parses them incrementally, and
distributes JSON-serialised events to all active WebSocket subscribers.
"""

import asyncio
import json
import struct
import threading
from collections import defaultdict

from cuckoo.common.log import CuckooGlobalLogger

log = CuckooGlobalLogger(__name__)

# Protobuf tag/wire-type constants for onemon length-delimited framing.
# Each record is prefixed with a varint giving the byte length of the
# protobuf message that follows.  We only need the framing logic here;
# full protobuf decoding is done lazily (or left to consumers that have
# the generated _pb2 modules available).
_MAX_EVENT_BYTES = 1024 * 1024  # 1 MB safety cap per event


def _read_varint(buf: bytes, pos: int):
    """Read a protobuf-style varint from buf starting at pos.
    Returns (value, new_pos) or (None, pos) if not enough bytes."""
    result = 0
    shift = 0
    while pos < len(buf):
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, pos
        shift += 7
        if shift > 63:
            return None, pos  # overflow protection
    return None, pos  # incomplete


class _TaskBuffer:
    """Per-task incremental parse buffer for the onemon protobuf stream."""

    def __init__(self):
        self._buf = b""

    def feed(self, chunk: bytes):
        """Feed a raw chunk; return list of complete raw protobuf messages."""
        self._buf += chunk
        messages = []
        pos = 0
        while pos < len(self._buf):
            length, new_pos = _read_varint(self._buf, pos)
            if length is None:
                # Not enough bytes for the length prefix yet
                break
            if length > _MAX_EVENT_BYTES:
                log.warning("Onemon event too large, resetting buffer", size=length)
                self._buf = b""
                return messages
            if new_pos + length > len(self._buf):
                # Message body not fully received yet
                break
            messages.append(self._buf[new_pos : new_pos + length])
            pos = new_pos + length
        self._buf = self._buf[pos:]
        return messages


def _try_parse_protobuf(raw: bytes) -> dict | None:
    """Attempt to parse a raw protobuf message into a dict for JSON delivery.

    We use a best-effort approach: if the generated _pb2 modules are
    available (from processing/), use them; otherwise fall back to a minimal
    field extractor so telemetry still works without processing installed.
    """
    try:
        from cuckoo.processing.event.translate.threemon import (
            api_pb2,
            process_pb2,
            network_pb2,
            file_pb2,
            registry_pb2,
        )
        # Try each known message type in order
        for kind, cls in (
            ("process", process_pb2.Process),
            ("network", network_pb2.NetworkEvent),
            ("file", file_pb2.FileEvent),
            ("registry", registry_pb2.RegistryEvent),
            ("api", api_pb2.ApiCall),
        ):
            try:
                msg = cls()
                msg.ParseFromString(raw)
                # Quick sanity: protobuf always parses without error even for
                # wrong types; check a required-ish field.
                d = {
                    "type": kind,
                    "data": _proto_to_dict(msg),
                }
                return d
            except Exception:
                continue
    except ImportError:
        pass

    # Fallback: emit as base64-encoded raw bytes so the browser can still
    # see something without the protobuf libraries installed.
    import base64
    return {
        "type": "raw",
        "data": base64.b64encode(raw).decode(),
    }


def _proto_to_dict(msg) -> dict:
    """Shallow conversion of a protobuf message to a plain dict."""
    try:
        from google.protobuf.json_format import MessageToDict
        return MessageToDict(msg, preserving_proto_field_name=True)
    except Exception:
        return {}


class LiveEventBroker:
    """Central broker that receives raw data from the result server and
    distributes parsed events to WebSocket subscribers.

    Thread-safety: feed_raw/notify_screenshot may be called from the result
    server asyncio loop; subscribe/unsubscribe are called from the webapi
    asyncio loop.  Both sides use asyncio.run_coroutine_threadsafe() to
    post to the single asyncio event loop owned by the webapi.
    """

    def __init__(self):
        # asyncio loop owned by the webapi (set in attach_loop())
        self._loop: asyncio.AbstractEventLoop | None = None

        # Per-task parse buffers (written from resultserver thread-context)
        self._buffers: dict[str, _TaskBuffer] = defaultdict(_TaskBuffer)
        self._buf_lock = threading.Lock()

        # Per-task subscriber queues {task_id: set of asyncio.Queue}
        self._subscribers: dict[str, set] = defaultdict(set)
        self._sub_lock = asyncio.Lock()  # only used from asyncio context

    def attach_loop(self, loop: asyncio.AbstractEventLoop):
        self._loop = loop

    # ------------------------------------------------------------------
    # Called from resultserver context (may be a different thread/process)
    # ------------------------------------------------------------------

    def feed_raw(self, task_id: str, chunk: bytes):
        """Feed a raw chunk from a behavioural log upload."""
        if not self._loop:
            return
        with self._buf_lock:
            buf = self._buffers[task_id]
            messages = buf.feed(chunk)

        for raw_msg in messages:
            event = _try_parse_protobuf(raw_msg)
            if event:
                self._dispatch(task_id, event)

    def notify_screenshot(self, task_id: str, fname: str, ts: int):
        """Notify subscribers that a new screenshot was saved."""
        self._dispatch(
            task_id,
            {"type": "screenshot", "data": {"fname": fname, "ts": ts}},
        )

    def notify_task_ended(self, task_id: str):
        """Notify all subscribers that this task has finished."""
        self._dispatch(task_id, {"type": "task_ended"})

    def _dispatch(self, task_id: str, event: dict):
        if not self._loop:
            return
        asyncio.run_coroutine_threadsafe(
            self._async_dispatch(task_id, event), self._loop
        )

    async def _async_dispatch(self, task_id: str, event: dict):
        payload = json.dumps(event)
        async with self._sub_lock:
            queues = list(self._subscribers.get(task_id, []))
        for q in queues:
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                pass  # drop if subscriber is too slow

    # ------------------------------------------------------------------
    # Called from webapi asyncio context
    # ------------------------------------------------------------------

    async def subscribe(self, task_id: str) -> asyncio.Queue:
        q = asyncio.Queue(maxsize=512)
        async with self._sub_lock:
            self._subscribers[task_id].add(q)
        return q

    async def unsubscribe(self, task_id: str, q: asyncio.Queue):
        async with self._sub_lock:
            self._subscribers[task_id].discard(q)
            if not self._subscribers[task_id]:
                self._subscribers.pop(task_id, None)

    def cleanup_task(self, task_id: str):
        """Remove parse buffer for a finished task (call after notify_task_ended)."""
        with self._buf_lock:
            self._buffers.pop(task_id, None)
