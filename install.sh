#!/usr/bin/env bash
# ============================================================
# iPod Sync — Raspberry Pi Installer
# ============================================================
# Run as root:  sudo bash install.sh
# ============================================================
set -euo pipefail

INSTALL_DIR="/opt/ipod-sync"
DATA_DIR="/var/lib/ipod-sync"
CACHE_DIR="/var/cache/ipod-sync/podcasts"
MOUNT_POINT="/media/ipod"
SERVICE_USER="ipod-sync"
PYTHON="python3"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'

info()    { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

[[ $EUID -ne 0 ]] && error "Run as root: sudo bash install.sh"

# ── Dependencies ────────────────────────────────────────────
info "Installing system packages…"
apt-get update -qq
apt-get install -y -qq \
    python3 python3-pip python3-venv \
    util-linux udev \
    2>/dev/null

# ── User & directories ───────────────────────────────────────
info "Creating service user '${SERVICE_USER}'…"
if ! id "${SERVICE_USER}" &>/dev/null; then
    useradd --system --no-create-home --shell /usr/sbin/nologin \
        --groups plugdev "${SERVICE_USER}" || true
fi

# Add to plugdev/disk so it can access block devices via blkid
usermod -aG plugdev,disk "${SERVICE_USER}" 2>/dev/null || true

info "Creating directories…"
mkdir -p "${INSTALL_DIR}" "${DATA_DIR}" "${CACHE_DIR}" "${MOUNT_POINT}"
chown "${SERVICE_USER}:${SERVICE_USER}" "${DATA_DIR}" "${CACHE_DIR}"
chmod 750 "${DATA_DIR}" "${CACHE_DIR}"

# ── Copy application ─────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
info "Copying application to ${INSTALL_DIR}…"
rsync -a --delete \
    --exclude '__pycache__' \
    --exclude '*.pyc' \
    --exclude '.git' \
    --exclude 'venv' \
    "${SCRIPT_DIR}/" "${INSTALL_DIR}/"

# ── Python virtual environment ───────────────────────────────
info "Creating Python virtual environment…"
"${PYTHON}" -m venv "${INSTALL_DIR}/venv"
"${INSTALL_DIR}/venv/bin/pip" install --upgrade pip -q
info "Installing Python dependencies…"
"${INSTALL_DIR}/venv/bin/pip" install -r "${INSTALL_DIR}/requirements.txt" -q

chown -R "${SERVICE_USER}:${SERVICE_USER}" "${INSTALL_DIR}"

# ── udev rules ───────────────────────────────────────────────
info "Installing udev rules…"
cp "${INSTALL_DIR}/90-ipod.rules" /etc/udev/rules.d/
udevadm control --reload-rules
udevadm trigger

# ── systemd mount unit ───────────────────────────────────────
info "Installing systemd mount unit…"
cp "${INSTALL_DIR}/media-ipod.mount" /etc/systemd/system/
systemctl daemon-reload

# ── systemd services ─────────────────────────────────────────
info "Installing systemd services…"
cp "${INSTALL_DIR}/ipod-sync.service" /etc/systemd/system/
cp "${INSTALL_DIR}/ipod-sync-daemon.service" /etc/systemd/system/

# Update paths in service files to reference INSTALL_DIR
sed -i "s|/opt/ipod-sync|${INSTALL_DIR}|g" /etc/systemd/system/ipod-sync.service
sed -i "s|/opt/ipod-sync|${INSTALL_DIR}|g" /etc/systemd/system/ipod-sync-daemon.service
sed -i "s|User=ipod-sync|User=${SERVICE_USER}|g" /etc/systemd/system/ipod-sync.service
sed -i "s|Group=ipod-sync|Group=${SERVICE_USER}|g" /etc/systemd/system/ipod-sync.service

systemctl daemon-reload
systemctl enable ipod-sync.service
systemctl enable ipod-sync-daemon.service

info "Starting services…"
systemctl restart ipod-sync.service
sleep 2
systemctl restart ipod-sync-daemon.service

# ── Done ─────────────────────────────────────────────────────
IP=$(hostname -I | awk '{print $1}')
echo
echo -e "${GREEN}╔══════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║       iPod Sync installed!               ║${NC}"
echo -e "${GREEN}╚══════════════════════════════════════════╝${NC}"
echo
echo -e "  Web UI:    ${YELLOW}http://${IP}:8000${NC}"
echo -e "  Data dir:  ${DATA_DIR}"
echo -e "  Cache dir: ${CACHE_DIR}"
echo -e "  Mount:     ${MOUNT_POINT}"
echo
echo -e "  Service status:"
systemctl is-active ipod-sync.service && echo -e "    ${GREEN}✓${NC} ipod-sync" || echo -e "    ${RED}✗${NC} ipod-sync"
systemctl is-active ipod-sync-daemon.service && echo -e "    ${GREEN}✓${NC} ipod-sync-daemon" || echo -e "    ${RED}✗${NC} ipod-sync-daemon"
echo
echo -e "  Next steps:"
echo -e "    1. Open the web UI and configure Jellyfin + Pinepods"
echo -e "    2. Add podcast subscriptions"
echo -e "    3. Plug in your iPod — sync will start automatically"
echo -e "    4. Or click 'Sync Now' in the web UI"
echo
echo -e "  Logs:  journalctl -fu ipod-sync"
