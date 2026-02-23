# Copyright (C) 2019-2024 Estonian Information System Authority.
# See the file 'LICENSE' for copying permission.

import time
import urllib.request
import urllib.error
import json

from rest_framework.response import Response
from rest_framework.views import APIView

from cuckoo.common.config import cfg
from cuckoo.common.livejwt import issue_token
from cuckoo.common import db, task as task_module


def _get_task_db(task_id):
    """Return the DB task object or None."""
    ses = db.dbms.session()
    try:
        return ses.query(db.Task).filter_by(id=task_id).first()
    finally:
        ses.close()


def _call_node_vnc_token(node_url: str, api_key: str, task_id: str) -> dict:
    """Call POST {node_url}/task/{task_id}/vnc-token on the node webapi.

    Returns the parsed JSON response or raises RuntimeError on failure.
    """
    url = f"{node_url.rstrip('/')}/task/{task_id}/vnc-token"
    req = urllib.request.Request(
        url,
        data=b"",
        method="POST",
        headers={
            "Authorization": f"Token {api_key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="ignore")
        raise RuntimeError(f"Node returned HTTP {e.code}: {body}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"Could not contact node: {e.reason}")


class LiveSessionView(APIView):
    """GET /api/analysis/<analysis_id>/task/<task_id>/live

    Validates that the task is currently running, requests VNC access from the
    node, issues a short-lived JWT, and returns all info needed by the browser
    to connect to live view.

    Response:
    {
      "task_id": "...",
      "jwt": "...",
      "expires_at": <unix_ts>,
      "telemetry_ws_url": "ws://host/ws/live/<node_name>/<task_id>?token=<jwt>",
      "vnc_ws_url": "ws://host/ws/vnc/<node_name>/<vnc_token>",  // or null
      "vnc_token": "..."  // or null
    }
    """

    def get(self, request, analysis_id, task_id):
        db_task = _get_task_db(task_id)
        if not db_task:
            return Response({"error": "Task not found"}, status=404)

        if db_task.state != task_module.States.RUNNING:
            return Response(
                {
                    "error": f"Task is not running (state: {db_task.state})",
                    "state": db_task.state,
                },
                status=409,
            )

        # Determine node API URL and key
        try:
            is_distributed = cfg("cuckoo.yaml", "cuckoo", "distributed", "enabled")
        except Exception:
            is_distributed = False

        node_name = "local"
        node_url = None
        api_key = None

        if is_distributed:
            # In distributed mode, try to find the node from config
            try:
                nodes = cfg("distributed.yaml", "remote_nodes")
                # Use the first configured node; a more advanced implementation
                # would track which node is running which task
                for name, node_cfg in nodes.items():
                    node_url = node_cfg["api_url"]
                    api_key = node_cfg["api_key"]
                    node_name = name
                    break
            except Exception:
                pass

        if not node_url:
            # Fallback: standalone mode, node runs on localhost
            try:
                node_api_port = cfg("cuckoo.yaml", "cuckoo", "node", "api_port")
            except Exception:
                node_api_port = 8090
            node_url = f"http://127.0.0.1:{node_api_port}"
            try:
                api_key = cfg("distributed.yaml", "node_settings", "api_key")
            except Exception:
                api_key = ""

        # Issue JWT
        secret = cfg("cuckoo.yaml", "live", "secret")
        ttl = 3600
        jwt = issue_token(task_id, secret, ttl=ttl)
        expires_at = int(time.time()) + ttl

        # Request VNC info from the node (best-effort)
        vnc_token = None
        vnc_port = None
        try:
            vnc_info = _call_node_vnc_token(node_url, api_key, task_id)
            vnc_token = vnc_info.get("vnc_token")
            vnc_port = vnc_info.get("vnc_port")
        except RuntimeError:
            pass  # VNC not available or not configured; telemetry still works

        # Determine WebSocket base URL from the incoming request
        # The NGINX config proxies /ws/vnc/ and /ws/live/ to the appropriate backends
        scheme = "wss" if request.is_secure() else "ws"
        host = request.get_host()

        telemetry_ws_url = (
            f"{scheme}://{host}/ws/live/{node_name}/{task_id}?token={jwt}"
        )

        vnc_ws_url = None
        if vnc_token:
            vnc_ws_url = f"{scheme}://{host}/ws/vnc/{node_name}/{vnc_token}"

        return Response(
            {
                "task_id": task_id,
                "jwt": jwt,
                "expires_at": expires_at,
                "telemetry_ws_url": telemetry_ws_url,
                "vnc_ws_url": vnc_ws_url,
                "vnc_token": vnc_token,
                "vnc_port": vnc_port,
            }
        )
