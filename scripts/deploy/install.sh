#!/usr/bin/env bash
# Cuckoo3 — first-time installation script
# Run as root on a fresh Ubuntu 22.04 VM.
# Usage: sudo bash install.sh [--force]
#
# --force  Overwrite existing config files (default: skip if present)

set -euo pipefail

# ---------------------------------------------------------------------------
# Variables — adjust before running
# ---------------------------------------------------------------------------
CUCKOO_USER=cuckoo
REPO_URL=https://github.com/ExyPnoze/cuckoo3
BRANCH=main
INSTALL_DIR=/home/cuckoo/cuckoo3
CWD=/home/cuckoo/.cuckoocwd
VM_SUBNET=192.168.30.0/24
BRIDGE_IP=192.168.30.1/24
SOCKET=/tmp/cuckoo3-rooter.sock
# ---------------------------------------------------------------------------

FORCE=false
for arg in "$@"; do
    [[ "$arg" == "--force" ]] && FORCE=true
done

# Must run as root
if [[ $EUID -ne 0 ]]; then
    echo "ERROR: This script must be run as root." >&2
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATES_DIR="$SCRIPT_DIR/conf-templates"

log() { echo "[install] $*"; }
log_skip() { echo "[install] SKIP (already exists): $1"; }

# ---------------------------------------------------------------------------
# 1. apt packages
# ---------------------------------------------------------------------------
log "Installing system packages..."
apt-get update -qq
apt-get install -y \
    python3.10 python3.10-venv python3.10-dev \
    qemu-system-x86 qemu-utils \
    tcpdump iptables iptables-persistent iproute2 bridge-utils \
    nginx uwsgi \
    git curl

# ---------------------------------------------------------------------------
# 2. User cuckoo
# ---------------------------------------------------------------------------
if ! id "$CUCKOO_USER" &>/dev/null; then
    log "Creating user $CUCKOO_USER..."
    useradd -m -s /bin/bash "$CUCKOO_USER"
else
    log "User $CUCKOO_USER already exists."
fi

# Allow tcpdump without root for cuckoo user
setcap cap_net_raw,cap_net_admin=eip "$(which tcpdump)" 2>/dev/null || true
usermod -aG pcap "$CUCKOO_USER" 2>/dev/null || true

# Add caller to cuckoo group (for socket access)
if [[ -n "${SUDO_USER:-}" ]]; then
    usermod -aG "$CUCKOO_USER" "$SUDO_USER"
    log "Added $SUDO_USER to group $CUCKOO_USER."
fi

# ---------------------------------------------------------------------------
# 3. Bridge br0
# ---------------------------------------------------------------------------
log "Configuring bridge br0..."
ip link add br0 type bridge 2>/dev/null || true
ip addr add "$BRIDGE_IP" dev br0 2>/dev/null || true
ip link set br0 up

# Persist bridge configuration
BR_CONF=/etc/network/interfaces.d/br0.conf
if [[ ! -f "$BR_CONF" ]] || [[ "$FORCE" == "true" ]]; then
    # Detect netplan vs ifupdown
    if command -v netplan &>/dev/null && [[ -d /etc/netplan ]]; then
        # netplan — write a separate yaml
        NETPLAN_FILE=/etc/netplan/99-cuckoo-br0.yaml
        cat > "$NETPLAN_FILE" <<NETPLAN
network:
  version: 2
  bridges:
    br0:
      addresses:
        - ${BRIDGE_IP}
      parameters:
        stp: false
        forward-delay: 0
NETPLAN
        netplan apply 2>/dev/null || true
        log "Written netplan config: $NETPLAN_FILE"
    else
        mkdir -p /etc/network/interfaces.d
        cat > "$BR_CONF" <<IFUPDOWN
auto br0
iface br0 inet static
    address ${BRIDGE_IP%/*}
    netmask 255.255.255.0
    bridge_ports none
    bridge_stp off
    bridge_fd 0
IFUPDOWN
        log "Written ifupdown config: $BR_CONF"
    fi
else
    log_skip "bridge config"
fi

# ---------------------------------------------------------------------------
# 4. ip_forward + NAT masquerading
# ---------------------------------------------------------------------------
log "Enabling IP forwarding and NAT..."
IFACE=$(ip route show default 2>/dev/null | awk '{print $5; exit}')
if [[ -z "$IFACE" ]]; then
    echo "ERROR: Could not detect default network interface." >&2
    exit 1
fi
log "Default interface detected: $IFACE"

sysctl -w net.ipv4.ip_forward=1
grep -qxF 'net.ipv4.ip_forward=1' /etc/sysctl.conf \
    || echo 'net.ipv4.ip_forward=1' >> /etc/sysctl.conf

# Idempotent: only add the MASQUERADE rule if it doesn't exist yet
if ! iptables -t nat -C POSTROUTING -s "$VM_SUBNET" -o "$IFACE" -j MASQUERADE 2>/dev/null; then
    iptables -t nat -A POSTROUTING -s "$VM_SUBNET" -o "$IFACE" -j MASQUERADE
fi
mkdir -p /etc/iptables
iptables-save > /etc/iptables/rules.v4

# ---------------------------------------------------------------------------
# 5. Clone repo + venv + install packages
# ---------------------------------------------------------------------------
if [[ ! -d "$INSTALL_DIR/.git" ]]; then
    log "Cloning $REPO_URL (branch $BRANCH) to $INSTALL_DIR..."
    sudo -u "$CUCKOO_USER" git clone -b "$BRANCH" "$REPO_URL" "$INSTALL_DIR"
else
    log "Repo already cloned at $INSTALL_DIR."
fi

if [[ ! -d "$INSTALL_DIR/venv" ]]; then
    log "Creating Python venv..."
    sudo -u "$CUCKOO_USER" python3.10 -m venv "$INSTALL_DIR/venv"
fi

log "Installing Cuckoo packages into venv..."
sudo -u "$CUCKOO_USER" bash -c "
    source $INSTALL_DIR/venv/bin/activate
    pip install --quiet --upgrade pip
    pip install --quiet uwsgi
    bash $INSTALL_DIR/install.sh
"

# ---------------------------------------------------------------------------
# 6. createcwd
# ---------------------------------------------------------------------------
if [[ ! -d "$CWD/conf" ]]; then
    log "Initializing Cuckoo working directory at $CWD..."
    sudo -u "$CUCKOO_USER" "$INSTALL_DIR/venv/bin/cuckoo" createcwd --cwd "$CWD"
else
    log "CWD already initialized at $CWD."
fi

# ---------------------------------------------------------------------------
# 7. Copy config templates with substitutions
# ---------------------------------------------------------------------------
LIVE_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")

copy_template() {
    local src="$1"
    local dst="$2"
    if [[ -f "$dst" ]] && [[ "$FORCE" != "true" ]]; then
        log_skip "$dst"
        return
    fi
    mkdir -p "$(dirname "$dst")"
    sed \
        -e "s|{{IFACE}}|$IFACE|g" \
        -e "s|{{LIVE_SECRET}}|$LIVE_SECRET|g" \
        -e "s|{{CWD}}|$CWD|g" \
        "$src" > "$dst"
    log "Written: $dst"
}

copy_template "$TEMPLATES_DIR/cuckoo.yaml"                   "$CWD/conf/cuckoo.yaml"
copy_template "$TEMPLATES_DIR/analysissettings.yaml"         "$CWD/conf/analysissettings.yaml"
copy_template "$TEMPLATES_DIR/node/routing.yaml"             "$CWD/conf/node/routing.yaml"
copy_template "$TEMPLATES_DIR/machineries/qemu.yaml"         "$CWD/conf/machineries/qemu.yaml"

chown -R "$CUCKOO_USER:$CUCKOO_USER" "$CWD"

# ---------------------------------------------------------------------------
# 8. nginx + uwsgi configs
# ---------------------------------------------------------------------------
log "Generating nginx and uwsgi configs..."
sudo -u "$CUCKOO_USER" bash -c "
    source $INSTALL_DIR/venv/bin/activate
    export CUCKOO_CWD=$CWD

    cuckoo --cwd $CWD web generateconfig --uwsgi > $CWD/conf/uwsgi-web.ini
    log_lines=\$(cuckoo --cwd $CWD web generateconfig --nginx)
    echo \"\$log_lines\" > /tmp/cuckoo-nginx-web.conf
    api_lines=\$(cuckoo --cwd $CWD api generateconfig --nginx)
    echo \"\$api_lines\" > /tmp/cuckoo-nginx-api.conf

    cuckoo --cwd $CWD web djangocommand collectstatic --noinput 2>/dev/null || true
" 2>/dev/null || log "Warning: config generation had errors (may need VMs first)."

if [[ -f /tmp/cuckoo-nginx-web.conf ]]; then
    cat /tmp/cuckoo-nginx-web.conf /tmp/cuckoo-nginx-api.conf \
        > /etc/nginx/sites-available/cuckoo 2>/dev/null || true
    rm -f /tmp/cuckoo-nginx-web.conf /tmp/cuckoo-nginx-api.conf
fi
ln -sf /etc/nginx/sites-available/cuckoo /etc/nginx/sites-enabled/cuckoo 2>/dev/null || true
rm -f /etc/nginx/sites-enabled/default 2>/dev/null || true
nginx -t 2>/dev/null && systemctl reload nginx 2>/dev/null || true

# ---------------------------------------------------------------------------
# 9. systemd service units
# ---------------------------------------------------------------------------
log "Creating systemd service units..."

cat > /etc/systemd/system/cuckoo-rooter.service <<SERVICE
[Unit]
Description=Cuckoo3 Rooter - network routing daemon
After=network.target

[Service]
Type=simple
ExecStart=$INSTALL_DIR/venv/bin/cuckoorooter \\
    --cwd $CWD \\
    --group $CUCKOO_USER \\
    --iptables /usr/sbin/iptables \\
    --ip /usr/sbin/ip \\
    $SOCKET
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
SERVICE

cat > /etc/systemd/system/cuckoo.service <<SERVICE
[Unit]
Description=Cuckoo3 malware analysis sandbox
After=network.target cuckoo-rooter.service
Requires=cuckoo-rooter.service

[Service]
Type=simple
User=$CUCKOO_USER
Group=$CUCKOO_USER
ExecStart=$INSTALL_DIR/venv/bin/cuckoo --cwd $CWD
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
SERVICE

cat > /etc/systemd/system/cuckoo-web.service <<SERVICE
[Unit]
Description=Cuckoo3 Web UI (uWSGI)
After=network.target

[Service]
Type=simple
User=$CUCKOO_USER
Group=$CUCKOO_USER
ExecStart=$INSTALL_DIR/venv/bin/uwsgi \\
    --ini $CWD/conf/uwsgi-web.ini
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
SERVICE

cat > /etc/systemd/system/cuckoo-api.service <<SERVICE
[Unit]
Description=Cuckoo3 REST API
After=network.target

[Service]
Type=simple
User=$CUCKOO_USER
Group=$CUCKOO_USER
ExecStart=$INSTALL_DIR/venv/bin/cuckoo api \\
    --cwd $CWD \\
    -h 127.0.0.1 -p 8090
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
SERVICE

# ---------------------------------------------------------------------------
# 10. Enable and start services
# ---------------------------------------------------------------------------
log "Enabling and starting services..."
systemctl daemon-reload
systemctl enable cuckoo-rooter cuckoo cuckoo-web cuckoo-api
systemctl start cuckoo-rooter

# cuckoo, cuckoo-web, cuckoo-api need VMs to be imported first — start but
# they may fail until VMs are available.
systemctl start cuckoo cuckoo-web cuckoo-api 2>/dev/null || true

log ""
log "===================================================================="
log "Installation complete."
log ""
log "Next steps:"
log "  1. Transfer VMs from WSL:"
log "     rsync -avz ~/.vmcloak/ cuckoo@<azure-vm>:~/.vmcloak/"
log "  2. Import VMs:"
log "     sudo -u cuckoo $INSTALL_DIR/venv/bin/cuckoo machine import qemu ~/.vmcloak/vms/"
log "  3. Restart services:"
log "     systemctl restart cuckoo cuckoo-web cuckoo-api"
log "  4. Take Azure snapshot."
log "===================================================================="
