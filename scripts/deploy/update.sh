#!/usr/bin/env bash
# Cuckoo3 — update script
# Pull a branch and reinstall packages without full teardown.
#
# Usage:
#   sudo bash update.sh               → pull current branch
#   sudo bash update.sh main          → switch to main + pull
#   sudo bash update.sh feature/xxx   → switch to feature branch + pull

set -euo pipefail

# ---------------------------------------------------------------------------
# Variables — must match install.sh
# ---------------------------------------------------------------------------
CUCKOO_USER=cuckoo
INSTALL_DIR=/home/cuckoo/cuckoo3
# ---------------------------------------------------------------------------

if [[ $EUID -ne 0 ]]; then
    echo "ERROR: This script must be run as root." >&2
    exit 1
fi

log() { echo "[update] $*"; }

TARGET_BRANCH="${1:-}"

# ---------------------------------------------------------------------------
# 1. Stop Cuckoo services (keep rooter running)
# ---------------------------------------------------------------------------
log "Stopping cuckoo, cuckoo-web, cuckoo-api..."
systemctl stop cuckoo cuckoo-web cuckoo-api 2>/dev/null || true

# ---------------------------------------------------------------------------
# 2. Git update
# ---------------------------------------------------------------------------
log "Fetching latest changes from origin..."
sudo -u "$CUCKOO_USER" git -C "$INSTALL_DIR" fetch origin

if [[ -n "$TARGET_BRANCH" ]]; then
    log "Switching to branch: $TARGET_BRANCH"
    sudo -u "$CUCKOO_USER" git -C "$INSTALL_DIR" checkout "$TARGET_BRANCH"
    sudo -u "$CUCKOO_USER" git -C "$INSTALL_DIR" pull origin "$TARGET_BRANCH"
else
    CURRENT_BRANCH=$(sudo -u "$CUCKOO_USER" git -C "$INSTALL_DIR" rev-parse --abbrev-ref HEAD)
    log "Pulling current branch: $CURRENT_BRANCH"
    sudo -u "$CUCKOO_USER" git -C "$INSTALL_DIR" pull
fi

CURRENT_BRANCH=$(sudo -u "$CUCKOO_USER" git -C "$INSTALL_DIR" rev-parse --abbrev-ref HEAD)
CURRENT_COMMIT=$(sudo -u "$CUCKOO_USER" git -C "$INSTALL_DIR" rev-parse --short HEAD)
log "Now at branch=$CURRENT_BRANCH commit=$CURRENT_COMMIT"

# ---------------------------------------------------------------------------
# 3. Reinstall packages (editable, dependency order)
# ---------------------------------------------------------------------------
log "Reinstalling Cuckoo packages into venv..."
sudo -u "$CUCKOO_USER" bash -c "
    source $INSTALL_DIR/venv/bin/activate
    pip install --quiet \
        -e $INSTALL_DIR/common \
        -e $INSTALL_DIR/processing \
        -e $INSTALL_DIR/machineries \
        -e $INSTALL_DIR/web \
        -e $INSTALL_DIR/node \
        -e $INSTALL_DIR/core
"

# ---------------------------------------------------------------------------
# 4. Restart services
# ---------------------------------------------------------------------------
log "Starting cuckoo, cuckoo-web, cuckoo-api..."
systemctl start cuckoo cuckoo-web cuckoo-api

# ---------------------------------------------------------------------------
# 5. Status
# ---------------------------------------------------------------------------
sleep 2
log ""
log "Service status:"
systemctl status cuckoo cuckoo-web cuckoo-api --no-pager -l 2>&1 | head -40

# Check rooter socket
if [[ -S /tmp/cuckoo3-rooter.sock ]]; then
    log "Rooter socket: OK (/tmp/cuckoo3-rooter.sock)"
else
    log "WARNING: Rooter socket not found at /tmp/cuckoo3-rooter.sock"
fi

log ""
log "Update complete. Branch: $CURRENT_BRANCH @ $CURRENT_COMMIT"
