# Live Sandbox

The live sandbox feature provides **real-time visibility into a running analysis** directly
from the web interface. An analyst can watch the Windows guest VM desktop via noVNC while
simultaneously seeing a live stream of behavioral events (processes, network connections,
file and registry activity) as they are captured by the monitor.

---

## Overview

```
┌─────────────────────────────────────────────────────┐
│                  Browser (analyst)                  │
│                                                     │
│   ┌─────────────────┐   ┌────────────────────────┐ │
│   │  noVNC canvas   │   │   Telemetry panels     │ │
│   │  (live desktop) │   │  ┌──────────────────┐  │ │
│   │                 │   │  │  Process tree    │  │ │
│   │  WebSocket VNC  │   │  ├──────────────────┤  │ │
│   │  (port 6080)    │   │  │  Network         │  │ │
│   │                 │   │  ├──────────────────┤  │ │
│   └─────────────────┘   │  │  Files           │  │ │
│                         │  ├──────────────────┤  │ │
│                         │  │  Registry        │  │ │
│                         │  └──────────────────┘  │ │
│                         │  WebSocket telemetry   │ │
│                         └────────────────────────┘ │
└─────────────────────────────────────────────────────┘
```

When an analysis is running, a **Live** button appears on the task page. Clicking it opens
the live view at `/analysis/<id>/task/<id>/live`.

---

## Architecture

### Data flow

```
Windows guest VM
│
├─ Threemon monitor (injected)
│    └─ Captures process/network/file/registry events
│         └─ Encodes as protobuf binary stream
│
└─ Cuckoo agent (running)
     └─ Uploads binary stream to Result Server (TCP 2042)

Result Server (node process)
│
└─ multiprocessing.Queue  ──────► LiveEventBroker (node process)
   ("raw", task_id, bytes)              │
                                        ├─ Parses protobuf incrementally
                                        ├─ Filters infrastructure noise
                                        └─ Dispatches JSON to WS subscribers

WebAPI (node, aiohttp)
│
└─ WebSocket endpoint  ◄──────── Browser (authenticated via JWT)
   /task/<id>/ws?token=<jwt>

websockify (subprocess)
│
└─ Proxies WS connections ◄────── Browser noVNC
   port 6080 → QEMU VNC port          (authenticated via VNC token)
```

### Components

| Component | Package | File | Role |
|-----------|---------|------|------|
| JWT utility | `common` | `livejwt.py` | HMAC-SHA256 token issue/verify |
| Event broker | `node` | `live.py` | Protobuf parsing, noise filtering, WS dispatch |
| VNC proxy | `node` | `vncproxy.py` | websockify process + token file management |
| Node WebAPI | `node` | `webapi.py` | WS endpoint `/task/<id>/ws`, VNC token endpoint |
| Node startup | `node` | `startup.py` | Wires broker + VNC proxy into node lifecycle |
| Web view | `web` | `live/views.py` | HTML page + session info API |
| Web API | `web` | `api/.../live/views.py` | JWT + WS URL issuance |
| Frontend | `web` | `static/js/live.js` | WS client, noVNC loader, event routing |
| Template | `web` | `templates/analysis/task_live.html.jinja2` | Split-panel layout |

---

## Security model

### JWT authentication

Every live session requires a **signed JWT token** (HMAC-SHA256) issued by the web layer
and verified by the node WebAPI. No WebSocket connection is accepted without a valid token.

```
Web layer                           Node WebAPI
─────────────────────────────────   ────────────────────────────────
issue_token(task_id, secret, ttl)   verify_token(token, secret)
  → header.payload.sig (base64url)    → raises LiveJWTError if invalid
                                        or expired (default TTL: 1 hour)
```

The `secret` is configured in `cuckoo.yaml` under `live.secret` and must be a random
32-byte hex string. It is generated automatically at install time by `scripts/deploy/install.sh`.

### VNC authentication

Each VNC session uses a **per-task random token** (UUID) stored in a websockify token file
(`~/.cuckoocwd/log/vnc_tokens.conf`). The file maps token → `127.0.0.1:<vnc_port>`.
websockify only forwards connections that present a registered token. The token is
invalidated when the task ends.

### WebSocket proxying

The web layer (nginx/Django) proxies WebSocket connections:

| Path | Proxied to |
|------|-----------|
| `ws://<host>/ws/live/<node>/<task_id>?token=<jwt>` | Node aiohttp WebAPI |
| `ws://<host>/ws/vnc/<node>?token=<vnc_token>` | websockify (port 6080) |

---

## Event pipeline

### Protobuf parsing

The Threemon monitor writes a binary stream to the result server. Each record:

```
┌────────┬────────┬──────────────────────┐
│ size   │  kind  │  protobuf payload    │
│ 3 bytes│ 1 byte │  size bytes          │
└────────┴────────┴──────────────────────┘
```

The `LiveEventBroker` maintains a per-task `_TaskBuffer` that accumulates raw bytes
and emits complete `(kind, data)` tuples as they arrive.

### Supported event kinds

| Kind byte | Type | Proto message | Fields sent to browser |
|-----------|------|---------------|----------------------|
| 1 | `process` | `Process` | pid, name (basename), image (full path), parent_pid |
| 2 | `registry` | `Registry` | operation, path, pid |
| 8 | `file` | `File` | operation, path, pid |
| 12 | `network` | `NetworkFlow` | src_ip, dst_ip, dst_port, proto (TCP/UDP/…), pid |
| 6 | `inject` | `Inject` | pid, dstpid, image |
| 9 | `mutant` | `Mutant` | action, name |

> **Note on IP encoding**: `srcip` and `dstip` in the protobuf are `fixed32` fields
> (big-endian 32-bit integers). They are converted to dotted-decimal strings via
> `socket.inet_ntoa(struct.pack(">I", val))` before being sent to the browser.

### Noise filtering

Events from known infrastructure processes and OS background services are suppressed
**before** being sent over the WebSocket to keep the live panel focused on malware activity.

**Filtered processes** (compared case-insensitively on basename):

| Process | Reason |
|---------|--------|
| `tmstage.exe` | Cuckoo stager binary |
| `smss.exe`, `csrss.exe`, `wininit.exe` | Windows Session bootstrap |
| `lsass.exe`, `winlogon.exe`, `services.exe` | Windows security/service layer |
| `svchost.exe` | Generic service host (30+ instances at boot) |
| `dwm.exe`, `fontdrvhost.exe` | Display stack |
| `taskhostw.exe`, `wmiprvse.exe` | Task/WMI infrastructure |
| `runtimebroker.exe`, `applicationframehost.exe`, `shellexperiencehost.exe`, `searchui.exe`, `backgroundtaskhost.exe` | UWP/shell infrastructure |
| `userinit.exe`, `sihost.exe` | Logon helpers |
| `spoolsv.exe` | Print spooler |
| `compattelrunner.exe`, `devicecensus.exe`, `sihclient.exe`, `usoclient.exe`, `wsqmcons.exe`, `msfeedssync.exe` | Windows telemetry/update |
| `sppsvc.exe`, `sppextcomobj.exe`, `slui.exe` | Windows licensing |

**Filtered network events:**

| Rule | Reason |
|------|--------|
| `dst_ip == resultserver_ip` | Agent uploading behavioral log to Cuckoo |
| `src_ip == resultserver_ip` | Response from result server |
| `224.x.x.x` or `239.x.x.x` | IPv4 multicast |
| `255.255.255.255` | Broadcast |
| `127.x.x.x` | Loopback |

The result server IP is read from `cuckoo.yaml` at startup and passed to the broker, so
the filter stays in sync with the configured value.

---

## Configuration

### `cuckoo.yaml`

```yaml
live:
  # Secret for signing JWT tokens. Generate with:
  # python3 -c "import secrets; print(secrets.token_hex(32))"
  secret: <32-byte hex string>

  # Port websockify listens on for noVNC connections. 0 = disabled.
  vnc_ws_port: 6080
```

### `conf/machineries/qemu.yaml`

Each machine that should support VNC live view needs a `vnc_port` entry:

```yaml
machines:
  win10_1:
    vnc_port: 5912   # QEMU VNC port; display = port - 5900
    # ... other machine settings
```

QEMU starts with `-vnc 127.0.0.1:<display>` automatically when `vnc_port` is set.
VNC is bound to localhost only; websockify handles the browser-facing WebSocket proxy.

### nginx proxy (generated by `cuckoo web generateconfig --nginx`)

The generated nginx config adds two proxy pass rules for WebSocket upgrade:

```nginx
# Telemetry WebSocket → node aiohttp
location /ws/live/ {
    proxy_pass http://127.0.0.1:8090;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
}

# VNC WebSocket → websockify
location /ws/vnc/ {
    proxy_pass http://127.0.0.1:6080;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
}
```

---

## Frontend (`live.js`)

The JavaScript controller is loaded by `task_live.html.jinja2` and handles:

1. **Bootstrap** — fetches `/api/analysis/<id>/task/<id>/live` to get JWT + WS URLs
2. **Telemetry WS** — connects, reconnects on drop (3 s), stops if task ends (HTTP 409)
3. **Event routing** — dispatches each JSON event to the correct panel
4. **noVNC** — dynamically imports `rfb.js` and initialises the RFB connection
5. **Task-ended banner** — shows "Analysis finished → View Report" when `task_ended` is received

**Panel layout:**

```
┌─────────────────────────────────────────────────────┐
│  Analysis #N  Task #N  [●connected]  Events: 42     │
│  [View Report]                                       │
├──────────────────────────┬──────────────────────────┤
│                          │  Process tree / API      │
│   noVNC canvas           │  (flex: 2)               │
│   (60% width)            ├──────────────────────────┤
│                          │  Network  (flex: 1.5)    │
│                          ├──────────────────────────┤
│                          │  Files    (flex: 1)      │
│                          ├──────────────────────────┤
│                          │  Registry (flex: 1)      │
└──────────────────────────┴──────────────────────────┘
```

Each panel keeps the **last 200 events** (older entries are discarded).

---

## Deployment

Full deployment instructions are in [`scripts/deploy/README.md`](../../scripts/deploy/README.md).

### Quick install (Azure VM, Ubuntu 22.04)

```bash
sudo bash scripts/deploy/install.sh
```

This script:
- Installs system dependencies (Python 3.10, QEMU, tcpdump, nginx, websockify…)
- Creates the `cuckoo` user and configures bridge `br0`
- Clones the repo, creates a venv, installs all packages
- Initialises the CWD and copies config templates (auto-generates `live.secret`)
- Creates 4 systemd services: `cuckoo-rooter`, `cuckoo`, `cuckoo-web`, `cuckoo-api`

### Required packages

In addition to standard Cuckoo dependencies:

```
websockify >= 0.10.0    # VNC WebSocket proxy
uwsgi                   # WSGI server (installed in venv)
```

### Updating to a branch

```bash
sudo bash scripts/deploy/update.sh [branch-name]
```

---

## Dependencies added

| Package | Dependency | Purpose |
|---------|-----------|---------|
| `node` | `websockify` (system) | VNC WebSocket proxy subprocess |
| `node` | `aiohttp` (already present) | Async WebSocket endpoint |
| `common` | — | `livejwt.py` (stdlib only: hmac, hashlib, base64) |
| `web` | — | `urllib.request` (stdlib) for node API calls |

`node/pyproject.toml` was updated to declare `websockify` as an optional dependency.

---

## Known limitations

| # | Limitation | Impact |
|---|-----------|--------|
| 1 | Process panel is a flat event list, not a rendered hierarchy tree | Parent-child relationships shown as `← ppid` only |
| 2 | MAX_ROWS = 200 per panel — older events are discarded | Long analyses may lose early events |
| 3 | Screenshots captured by result server but not displayed in live panel | Screenshot data is available, display not implemented |
| 4 | Single-node only (distributed mode picks first node) | Multi-node deployments always route to node 0 |
| 5 | VNC requires QEMU with `-vnc` support | Proxmox/KVM machinery backends not yet wired |
