# Copyright (C) 2019-2024 Estonian Information System Authority.
# See the file 'LICENSE' for copying permission.

import json
import time
import urllib.request
import urllib.error

from django.http import JsonResponse
from django.views import View
from django.views.generic import TemplateView

from cuckoo.common.config import cfg
from cuckoo.common.livejwt import issue_token
from cuckoo.common import db, task as task_module


class TaskLivePageView(TemplateView):
    template_name = "analysis/task_live.html.jinja2"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["analysis_id"] = kwargs.get("analysis_id", "")
        ctx["task_id"] = kwargs.get("task_id", "")
        return ctx


class LiveSessionApiView(View):
    """GET /api/analysis/<analysis_id>/task/<task_id>/live

    Returns JWT + WebSocket URLs for the live sandbox view.
    """

    def get(self, request, analysis_id, task_id):
        ses = db.dbms.session()
        try:
            db_task = ses.query(db.Task).filter_by(id=task_id).first()
        finally:
            ses.close()

        if not db_task:
            return JsonResponse({"error": "Task not found"}, status=404)

        if db_task.state != task_module.States.RUNNING:
            return JsonResponse(
                {
                    "error": f"Task is not running (state: {db_task.state})",
                    "state": db_task.state,
                },
                status=409,
            )

        # Determine node API URL
        try:
            is_distributed = cfg("cuckoo.yaml", "cuckoo", "distributed", "enabled")
        except Exception:
            is_distributed = False

        node_name = "local"
        node_url = None
        api_key = None

        if is_distributed:
            try:
                nodes = cfg("distributed.yaml", "remote_nodes")
                for name, node_cfg in nodes.items():
                    node_url = node_cfg["api_url"]
                    api_key = node_cfg["api_key"]
                    node_name = name
                    break
            except Exception:
                pass

        if not node_url:
            try:
                node_api_port = cfg("cuckoo.yaml", "node", "api_port")
            except Exception:
                node_api_port = 8090
            node_url = f"http://127.0.0.1:{node_api_port}"
            try:
                api_key = cfg("distributed.yaml", "node_settings", "api_key")
            except Exception:
                api_key = ""

        secret = cfg("cuckoo.yaml", "live", "secret")
        ttl = 3600
        jwt = issue_token(task_id, secret, ttl=ttl)
        expires_at = int(time.time()) + ttl

        # Request VNC info from the node (best-effort)
        vnc_token = None
        vnc_port = None
        try:
            url = f"{node_url.rstrip('/')}/task/{task_id}/vnc-token"
            req = urllib.request.Request(
                url, data=b"", method="POST",
                headers={"Authorization": f"Token {api_key or ''}",
                         "Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                vnc_info = json.loads(resp.read())
                vnc_token = vnc_info.get("vnc_token")
                vnc_port = vnc_info.get("vnc_port")
        except Exception:
            pass

        scheme = "wss" if request.is_secure() else "ws"
        host = request.get_host()

        return JsonResponse({
            "task_id": task_id,
            "jwt": jwt,
            "expires_at": expires_at,
            "telemetry_ws_url": f"{scheme}://{host}/ws/live/{node_name}/{task_id}?token={jwt}",
            "vnc_ws_url": f"{scheme}://{host}/ws/vnc/{node_name}?token={vnc_token}" if vnc_token else None,
            "vnc_token": vnc_token,
            "vnc_port": vnc_port,
        })
