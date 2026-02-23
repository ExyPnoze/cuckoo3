# Copyright (C) 2019-2024 Estonian Information System Authority.
# See the file 'LICENSE' for copying permission.

import os
import subprocess
import tempfile
import threading
from pathlib import Path

from cuckoo.common.log import CuckooGlobalLogger

log = CuckooGlobalLogger(__name__)


class VNCTokenManager:
    """Manages the websockify token file that maps JWT tokens to VNC ports.

    websockify --token-plugin TokenFile reads a file where each line is:
        <token>: <host>:<port>

    We keep a dict in memory and rewrite the file atomically on every change.
    """

    def __init__(self, token_file_path: str):
        self._path = token_file_path
        self._tokens = {}  # token -> vnc_port
        self._lock = threading.Lock()
        self._write_token_file()

    def add(self, task_id: str, vnc_port: int, vnc_token: str):
        """Register a VNC token for a running task."""
        if not vnc_port or not vnc_token:
            return
        with self._lock:
            self._tokens[vnc_token] = vnc_port
            log.debug(
                "VNC token registered",
                task_id=task_id,
                vnc_port=vnc_port,
            )
            self._write_token_file()

    def remove(self, vnc_token: str):
        """Remove a VNC token (task finished or session ended)."""
        with self._lock:
            removed = self._tokens.pop(vnc_token, None)
            if removed is not None:
                self._write_token_file()

    def remove_by_task(self, task_id: str, vnc_token: str):
        """Convenience: remove by task_id (uses vnc_token to look up)."""
        self.remove(vnc_token)

    def _write_token_file(self):
        """Atomically rewrite the token file."""
        dir_path = str(Path(self._path).parent)
        try:
            fd, tmp = tempfile.mkstemp(dir=dir_path, prefix=".vnctokens_")
            try:
                with os.fdopen(fd, "w") as fp:
                    for token, port in self._tokens.items():
                        fp.write(f"{token}: 127.0.0.1:{port}\n")
                os.replace(tmp, self._path)
            except Exception:
                os.unlink(tmp)
                raise
        except OSError as e:
            log.error("Failed to write VNC token file", path=self._path, error=e)


class WebsockifyProcess:
    """Manages the websockify subprocess that proxies WebSocket connections
    to QEMU VNC ports using a token file for authentication."""

    def __init__(self, listen_port: int, token_file_path: str):
        self._listen_port = listen_port
        self._token_file = token_file_path
        self._proc = None
        self._lock = threading.Lock()

    def start(self):
        with self._lock:
            if self._proc and self._proc.poll() is None:
                return

            cmd = [
                "websockify",
                f"0.0.0.0:{self._listen_port}",
                "--token-plugin=TokenFile",
                f"--token-source={self._token_file}",
                "--heartbeat=30",
            ]
            try:
                self._proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                )
                log.info(
                    "websockify started",
                    port=self._listen_port,
                    token_file=self._token_file,
                    pid=self._proc.pid,
                )
            except FileNotFoundError:
                log.error(
                    "websockify binary not found. Install websockify>=0.10.0 "
                    "to enable VNC live view."
                )
            except OSError as e:
                log.error("Failed to start websockify", error=e)

    def stop(self):
        with self._lock:
            if self._proc:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
                log.info("websockify stopped")
                self._proc = None
