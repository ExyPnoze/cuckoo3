# Copyright (C) 2019-2024 Estonian Information System Authority.
# See the file 'LICENSE' for copying permission.

"""Live telemetry broker for real-time sandbox event streaming.

The LiveEventBroker receives raw bytes from the result server (behavioural
log uploads) and screenshots notifications, parses them incrementally, and
distributes JSON-serialised events to all active WebSocket subscribers.
"""

import asyncio
import json
import threading
from collections import defaultdict

from cuckoo.common.log import CuckooGlobalLogger

log = CuckooGlobalLogger(__name__)

_MAX_EVENT_BYTES = 1024 * 1024  # 1 MB safety cap per event

# Threemon event kind byte -> (friendly_name, pb2_module_attr, pb2_class_name)
_KIND_MAP = {
    1:  ("process",  "process_pb2",  "Process"),
    2:  ("registry", "registry_pb2", "Registry"),
    8:  ("file",     "file_pb2",     "File"),
    12: ("network",  "network_pb2",  "NetworkFlow"),
    6:  ("inject",   "inject_pb2",   "Inject"),
    9:  ("mutant",   "mutant_pb2",   "Mutant"),
}


class _TaskBuffer:
    """Per-task incremental parse buffer for the threemon binary stream.

    Each record in the stream is:
        header[0..2]  : 3-byte little-endian data size
        header[3]     : 1-byte event kind
        data[0..size] : protobuf-encoded event body
    """

    def __init__(self):
        self._buf = b""

    def feed(self, chunk: bytes):
        """Feed a raw chunk; return list of (kind, data) tuples."""
        self._buf += chunk
        messages = []
        pos = 0
        while pos + 4 <= len(self._buf):
            header = self._buf[pos:pos + 4]
            data_size = header[0] + header[1] * 256 + header[2] * 65536
            kind = header[3]

            if data_size > _MAX_EVENT_BYTES:
                log.warning(
                    "Threemon event too large, resetting buffer", size=data_size
                )
                self._buf = b""
                return messages

            if pos + 4 + data_size > len(self._buf):
                break  # incomplete message, wait for more data

            messages.append((kind, self._buf[pos + 4: pos + 4 + data_size]))
            pos += 4 + data_size

        self._buf = self._buf[pos:]
        return messages


def _parse_event(kind: int, raw: bytes):
    """Parse a threemon protobuf event into a JSON-ready dict.

    Returns None for unknown/unsupported kinds.
    """
    info = _KIND_MAP.get(kind)
    if not info:
        return None

    kind_name, mod_attr, cls_name = info

    try:
        from cuckoo.processing.event.translate import threemon as _threemon_pkg
        mod = getattr(_threemon_pkg, mod_attr)
        cls = getattr(mod, cls_name)
        msg = cls()
        msg.ParseFromString(raw)
        data = _proto_to_dict(msg)
        data = _normalize(kind_name, data)
        return {"type": kind_name, "data": data}
    except Exception:
        return None


def _proto_to_dict(msg) -> dict:
    """Shallow conversion of a protobuf message to a plain dict."""
    try:
        from google.protobuf.json_format import MessageToDict
        return MessageToDict(msg, preserving_proto_field_name=True)
    except Exception:
        return {}


def _normalize(kind_name: str, d: dict) -> dict:
    """Map raw protobuf field names to the field names live.js expects."""
    if kind_name == "process":
        return {
            "pid":        d.get("pid"),
            "image":      d.get("image", ""),
            "parent_pid": d.get("ppid"),
            "command":    d.get("command", ""),
        }
    if kind_name == "network":
        return {
            "src_ip":   d.get("srcip", ""),
            "dst_ip":   d.get("dstip", ""),
            "dst_port": d.get("dstport"),
            "proto":    d.get("proto"),
            "pid":      d.get("pid"),
        }
    if kind_name == "file":
        return {
            "operation": str(d.get("kind", "")),
            "path":      d.get("srcpath", ""),
            "pid":       d.get("pid"),
        }
    if kind_name == "registry":
        return {
            "operation": str(d.get("kind", "")),
            "path":      d.get("path", ""),
            "pid":       d.get("pid"),
        }
    if kind_name == "inject":
        return {
            "pid":    d.get("srcpid"),
            "dstpid": d.get("dstpid"),
            "image":  f"inject→{d.get('dstpid', '?')}",
        }
    return d


class LiveEventBroker:
    """Central broker that receives raw data from the result server and
    distributes parsed events to WebSocket subscribers.

    Thread-safety: feed_raw/notify_screenshot may be called from the drain
    thread; subscribe/unsubscribe are called from the webapi asyncio loop.
    Both sides use asyncio.run_coroutine_threadsafe() to post to the single
    asyncio event loop owned by the webapi.
    """

    def __init__(self):
        self._loop: asyncio.AbstractEventLoop | None = None

        # Per-task parse buffers
        self._buffers: dict[str, _TaskBuffer] = defaultdict(_TaskBuffer)
        self._buf_lock = threading.Lock()

        # Per-task subscriber queues {task_id: set of asyncio.Queue}
        self._subscribers: dict[str, set] = defaultdict(set)
        self._sub_lock = asyncio.Lock()  # only used from asyncio context

    def attach_loop(self, loop: asyncio.AbstractEventLoop):
        self._loop = loop

    # ------------------------------------------------------------------
    # Called from drain thread context
    # ------------------------------------------------------------------

    def feed_raw(self, task_id: str, chunk: bytes):
        """Feed a raw chunk from a behavioural log upload."""
        if not self._loop:
            return
        with self._buf_lock:
            buf = self._buffers[task_id]
            messages = buf.feed(chunk)

        for kind, raw in messages:
            event = _parse_event(kind, raw)
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
        """Remove parse buffer for a finished task."""
        with self._buf_lock:
            self._buffers.pop(task_id, None)
