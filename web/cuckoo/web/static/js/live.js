/**
 * live.js — Cuckoo3 Live Sandbox view
 *
 * Responsibilities:
 *  1. Fetch live session info from the REST API
 *  2. Connect to the telemetry WebSocket and route events to the correct panel
 *  3. Optionally load noVNC and connect to the VNC WebSocket
 */

"use strict";

(function () {

  /* -----------------------------------------------------------------------
   * State
   * --------------------------------------------------------------------- */
  const analysisId   = window.LIVE_ANALYSIS_ID;
  const taskId       = window.LIVE_TASK_ID;
  const apiBase      = window.LIVE_API_BASE;

  let ws             = null;
  let rfb            = null;   // noVNC RFB object
  let totalEvents    = 0;
  const counts       = { process: 0, network: 0, file: 0, registry: 0 };
  const MAX_ROWS     = 200;    // keep only last N rows per panel

  /* -----------------------------------------------------------------------
   * DOM helpers
   * --------------------------------------------------------------------- */
  function el(id)           { return document.getElementById(id); }
  function setWsStatus(cls, txt) {
    const e = el("ws-indicator");
    e.className = cls;
    e.textContent = txt;
  }
  function setVncStatus(cls, txt) {
    const e = el("vnc-status");
    e.className = cls;
    e.textContent = txt;
  }
  function prependRow(listId, html) {
    const list = el(listId);
    const div = document.createElement("div");
    div.className = "tele-item";
    div.innerHTML = html;
    list.prepend(div);
    // Trim old entries
    while (list.children.length > MAX_ROWS) {
      list.removeChild(list.lastChild);
    }
  }
  function updateCount(type) {
    counts[type] = (counts[type] || 0) + 1;
    const cntEl = el("cnt-" + type);
    if (cntEl) cntEl.textContent = counts[type];
    totalEvents++;
    el("event-count-total").textContent = "Events: " + totalEvents;
  }

  /* -----------------------------------------------------------------------
   * Event routing
   * --------------------------------------------------------------------- */
  function handleEvent(evt) {
    const t = evt.type;
    const d = evt.data || {};

    if (t === "task_ended") {
      setWsStatus("disconnected", "task ended — redirecting…");
      setVncStatus("error", "task ended");
      if (ws) ws.close();
      if (rfb) rfb.disconnect();
      setTimeout(() => {
        window.location.href = "/analysis/" + analysisId + "/task/" + taskId;
      }, 2000);
      return;
    }

    if (t === "screenshot") {
      // Could display screenshot thumbnail here if desired
      return;
    }

    if (t === "process") {
      const pid  = d.pid   || d.process_id || "?";
      const name = d.image || d.name       || "unknown";
      const ppid = d.parent_pid ? ` [ppid:${d.parent_pid}]` : "";
      prependRow("list-process",
        `<span class="ev-pid">${pid}</span> `+
        `<span class="ev-call">${escHtml(name)}</span>`+
        `<span style="color:#777">${ppid}</span>`);
      updateCount("process");
      return;
    }

    if (t === "api") {
      const pid  = d.process_id || "?";
      const call = d.api_name   || d.call || "?";
      const ret  = d.return_value !== undefined ? ` → ${d.return_value}` : "";
      prependRow("list-process",
        `<span class="ev-pid">${pid}</span> `+
        `<span class="ev-call">${escHtml(call)}</span>`+
        `<span style="color:#777;font-size:0.68rem">${escHtml(ret)}</span>`);
      updateCount("process");
      return;
    }

    if (t === "network") {
      const src = d.src_ip  || d.source      || "";
      const dst = d.dst_ip  || d.destination || "";
      const port = d.dst_port || d.port       || "";
      const proto = d.proto || d.protocol    || "";
      prependRow("list-network",
        `<span class="ev-net">${escHtml(proto)}</span> `+
        `<span>${escHtml(src)}</span> → `+
        `<span>${escHtml(dst)}${port ? ":"+port : ""}</span>`);
      updateCount("network");
      return;
    }

    if (t === "file") {
      const op   = d.operation || d.type || "?";
      const path = d.path      || d.file || "";
      prependRow("list-file",
        `<span style="color:#888">${escHtml(op)}</span> `+
        `<span class="ev-path">${escHtml(path)}</span>`);
      updateCount("file");
      return;
    }

    if (t === "registry") {
      const op  = d.operation || d.type || "?";
      const key = d.key       || d.path || "";
      prependRow("list-registry",
        `<span style="color:#888">${escHtml(op)}</span> `+
        `<span class="ev-reg">${escHtml(key)}</span>`);
      updateCount("registry");
      return;
    }

    if (t === "raw") {
      // Raw bytes event — show in process panel as fallback
      prependRow("list-process",
        `<span style="color:#555">[raw] ${escHtml((d || "").slice(0,60))}</span>`);
      updateCount("process");
    }
  }

  function escHtml(str) {
    return String(str)
      .replace(/&/g,"&amp;")
      .replace(/</g,"&lt;")
      .replace(/>/g,"&gt;")
      .replace(/"/g,"&quot;");
  }

  /* -----------------------------------------------------------------------
   * Telemetry WebSocket
   * --------------------------------------------------------------------- */
  function connectTelemetry(wsUrl) {
    setWsStatus("connecting", "connecting…");
    ws = new WebSocket(wsUrl);

    ws.onopen = () => {
      setWsStatus("connected", "connected");
    };

    ws.onmessage = (msg) => {
      try {
        const evt = JSON.parse(msg.data);
        handleEvent(evt);
      } catch (e) {
        console.warn("Failed to parse live event", msg.data, e);
      }
    };

    ws.onerror = () => {
      setWsStatus("disconnected", "error");
    };

    ws.onclose = (ev) => {
      if (ev.code !== 1000) {
        setWsStatus("disconnected", "disconnected (code " + ev.code + ")");
        // Reconnect after 3 seconds if not intentional close
        setTimeout(() => connectTelemetry(wsUrl), 3000);
      }
    };
  }

  /* -----------------------------------------------------------------------
   * noVNC
   * --------------------------------------------------------------------- */
  function loadNoVNC(vncWsUrl, vncPassword) {
    // Try to load noVNC from /static/novnc/core/rfb.js
    const script = document.createElement("script");
    script.type = "module";
    script.textContent = `
      import RFB from "/static/novnc/core/rfb.js";
      window._initRFB = function(wsUrl, password) {
        const container = document.getElementById("vnc-canvas-container");
        document.getElementById("vnc-placeholder").style.display = "none";
        try {
          const rfb = new RFB(container, wsUrl);
          rfb.scaleViewport = true;
          rfb.resizeSession = false;
          rfb.addEventListener("connect", () => {
            document.getElementById("vnc-status").className = "connected";
            document.getElementById("vnc-status").textContent = "connected";
          });
          rfb.addEventListener("disconnect", (ev) => {
            document.getElementById("vnc-status").className = "error";
            document.getElementById("vnc-status").textContent =
              ev.detail.clean ? "disconnected" : "lost";
          });
          rfb.addEventListener("credentialsrequired", () => {
            if (password) {
              rfb.sendCredentials({ password: password });
            }
          });
          window._rfbInstance = rfb;
        } catch(e) {
          document.getElementById("vnc-status").className = "error";
          document.getElementById("vnc-status").textContent = "failed: " + e;
        }
      };
      window._initRFB(${JSON.stringify(vncWsUrl)}, ${JSON.stringify(vncPassword || null)});
    `;
    document.head.appendChild(script);
    setVncStatus("connecting", "connecting…");
  }

  /* -----------------------------------------------------------------------
   * Bootstrap: fetch session info then connect
   * --------------------------------------------------------------------- */
  async function init() {
    let sessionInfo;
    try {
      const resp = await fetch(apiBase + "/live", {
        headers: { "X-CSRFToken": window.csrf_token || "" },
      });
      if (!resp.ok) {
        const body = await resp.json().catch(() => ({}));
        const msg = body.error || resp.statusText;
        setWsStatus("disconnected", "API error: " + msg);
        if (resp.status === 409) {
          // Task not running yet or already finished
          el("live-ended-notice").style.display = "";
          el("live-ended-notice").textContent = "Task is not running (state: " + (body.state || "?") + ")";
        }
        setVncStatus("unavailable", "unavailable");
        return;
      }
      sessionInfo = await resp.json();
    } catch (e) {
      setWsStatus("disconnected", "fetch error: " + e);
      return;
    }

    const { telemetry_ws_url, vnc_ws_url, vnc_token, jwt } = sessionInfo;

    if (telemetry_ws_url) {
      connectTelemetry(telemetry_ws_url);
    } else {
      setWsStatus("disconnected", "no telemetry URL");
    }

    if (vnc_ws_url) {
      loadNoVNC(vnc_ws_url, vnc_token);
    } else {
      setVncStatus("unavailable", "VNC not available");
      el("vnc-placeholder").textContent = "VNC not available for this session.";
    }
  }

  // Start when DOM is ready
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }

})();
