# Cuckoo3 — Azure Deployment Guide

Automated scripts for deploying Cuckoo3 on Azure from the `main` branch (stable),
with support for updating to any feature branch via `update.sh`.

---

## Prerequisites

### Azure VM

| Setting | Recommended |
|---------|-------------|
| Image | Ubuntu 22.04 LTS |
| Size | Standard_D4s_v3 or larger (4 vCPUs, 16 GB RAM) |
| Nested virtualization | Required — use Dv3/Dsv3/Ev3/Esv3 series |
| OS disk | 64 GB minimum (128 GB recommended) |
| Data disk | 256 GB+ for VM images |

Enable nested virtualization by selecting a VM size that supports it
(e.g. `Standard_D4s_v3`). Verify after boot:

```bash
grep -c vmx /proc/cpuinfo   # should return > 0
```

### Local machine (WSL)

- VMCloak VMs built and located at `~/.vmcloak/vms/`
- SSH access to the Azure VM configured

---

## Installation order

### Step 1 — Run install.sh on the Azure VM

```bash
# Copy scripts to the VM first
scp -r scripts/deploy/ cuckoo@<azure-vm>:~/deploy/

# SSH in as a sudo-capable user
ssh <admin-user>@<azure-vm>

# Run the installer
sudo bash ~/deploy/install.sh
```

The script is **idempotent** — safe to re-run. Use `--force` to overwrite
existing config files:

```bash
sudo bash ~/deploy/install.sh --force
```

What it does:
1. Installs system packages (Python 3.10, QEMU, tcpdump, iptables, nginx, git…)
2. Creates the `cuckoo` user
3. Configures bridge `br0` (192.168.30.1/24) with persistence
4. Enables IP forwarding + NAT masquerading via iptables
5. Clones the repo, creates venv, installs all Cuckoo packages
6. Initializes the CWD (`~/.cuckoocwd/`)
7. Copies config templates with auto-substituted placeholders
8. Generates nginx + uwsgi configs
9. Creates and enables 4 systemd services

### Step 2 — Transfer VMs from WSL

```bash
rsync -avz --progress ~/.vmcloak/ cuckoo@<azure-vm>:~/.vmcloak/
```

### Step 3 — Import VMs

```bash
ssh cuckoo@<azure-vm>
source ~/cuckoo3/venv/bin/activate
cuckoo machine import qemu ~/.vmcloak/vms/
```

### Step 4 — Start services and verify

```bash
sudo systemctl restart cuckoo cuckoo-web cuckoo-api
sudo systemctl status cuckoo cuckoo-rooter cuckoo-web cuckoo-api
```

### Step 5 — Take Azure snapshot

Take an Azure VM snapshot at this point. This snapshot is your clean baseline
to restore to after a failed experiment.

---

## Updating to a branch

Use `update.sh` to switch branches and reinstall without rebuilding from scratch.
The rooter service keeps running during updates.

```bash
# Pull current branch
sudo bash ~/deploy/update.sh

# Switch to main and pull
sudo bash ~/deploy/update.sh main

# Switch to a feature branch
sudo bash ~/deploy/update.sh feature/rooter-routing
```

---

## Services

| Service | User | Description |
|---------|------|-------------|
| `cuckoo-rooter` | root | Network routing daemon (unix socket) |
| `cuckoo` | cuckoo | Main analysis controller |
| `cuckoo-web` | cuckoo | Web UI via uWSGI |
| `cuckoo-api` | cuckoo | REST API (port 8090) |

```bash
# View logs
journalctl -u cuckoo -f
journalctl -u cuckoo-rooter -f

# Restart all
sudo systemctl restart cuckoo cuckoo-web cuckoo-api cuckoo-rooter
```

---

## Config templates

Templates live in `conf-templates/` and are copied to `~/.cuckoocwd/conf/`
during installation. Placeholders substituted automatically:

| Placeholder | Value |
|-------------|-------|
| `{{LIVE_SECRET}}` | Random 32-byte hex secret (generated once at install) |
| `{{IFACE}}` | Auto-detected default network interface |
| `{{CWD}}` | Path to the Cuckoo working directory |

To regenerate configs (e.g. after changing templates):

```bash
sudo bash ~/deploy/install.sh --force
```

---

## Troubleshooting

**Rooter socket missing:**
```bash
sudo systemctl status cuckoo-rooter
sudo journalctl -u cuckoo-rooter -n 50
```

**VMs not found:**
```bash
cuckoo --cwd ~/.cuckoocwd machine list
```

**Bridge not up after reboot:**
```bash
ip link show br0
sudo ip link set br0 up
sudo ip addr add 192.168.30.1/24 dev br0
```
