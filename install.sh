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
    rsync udisks2 util-linux udev \
    2>/dev/null

# GPIO backend for optional hardware buttons/LEDs — best-effort, since
# this package isn't available on every Raspberry Pi OS release (or on
# non-Pi hosts). GPIO controls just won't work if it's missing.
apt-get install -y -qq python3-lgpio 2>/dev/null \
    || warn "python3-lgpio not available — GPIO buttons/LEDs won't work until it's installed"

# ── User & directories ───────────────────────────────────────
info "Creating service user '${SERVICE_USER}'…"
if ! id "${SERVICE_USER}" &>/dev/null; then
    useradd --system --no-create-home --shell /usr/sbin/nologin \
        --groups plugdev "${SERVICE_USER}" || true
fi

# plugdev  → udisks2 mount/unmount without root
# disk     → blkid can read device labels
# gpio     → access /dev/gpiochip* for the optional hardware buttons/LEDs
usermod -aG plugdev,disk,gpio "${SERVICE_USER}" 2>/dev/null || true

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
# --system-site-packages lets the venv's pip-installed gpiozero see the
# apt-installed python3-lgpio GPIO backend, which needs to be compiled
# against the Pi's kernel headers and isn't reliable to pip-install fresh.
"${PYTHON}" -m venv --system-site-packages "${INSTALL_DIR}/venv"
"${INSTALL_DIR}/venv/bin/pip" install --upgrade pip -q
info "Installing Python dependencies…"
"${INSTALL_DIR}/venv/bin/pip" install -r "${INSTALL_DIR}/requirements.txt" -q

chown -R "${SERVICE_USER}:${SERVICE_USER}" "${INSTALL_DIR}"

# ── sudoers rules ───────────────────────────────────────────
# Allow the service user to unmount and run blkid without a password.
# These are tightly scoped — no general sudo access is granted.
info "Installing sudoers rules…"
SUDOERS_FILE="/etc/sudoers.d/ipod-sync"
tee "${SUDOERS_FILE}" > /dev/null <<EOF
# ipod-sync: allow safe mount/eject and device label detection
${SERVICE_USER} ALL=(ALL) NOPASSWD: /usr/bin/udisksctl mount -b *
${SERVICE_USER} ALL=(ALL) NOPASSWD: /usr/bin/udisksctl unmount -b *
${SERVICE_USER} ALL=(ALL) NOPASSWD: /usr/bin/udisksctl power-off -b *
${SERVICE_USER} ALL=(ALL) NOPASSWD: /bin/umount ${MOUNT_POINT}
${SERVICE_USER} ALL=(ALL) NOPASSWD: /usr/sbin/blkid
${SERVICE_USER} ALL=(ALL) NOPASSWD: /sbin/blkid
EOF
chmod 440 "${SUDOERS_FILE}"
# Validate — will abort if syntax is wrong
visudo -c -f "${SUDOERS_FILE}" || { rm -f "${SUDOERS_FILE}"; error "sudoers validation failed"; }

# ── udev rules ───────────────────────────────────────────────
info "Installing udev rules…"
cp "${INSTALL_DIR}/90-ipod.rules" /etc/udev/rules.d/
udevadm control --reload-rules
udevadm trigger

# ── systemd mount unit (optional — Pi OS udisks2 usually auto-mounts) ───
# Only install if /media/ipod does not already get auto-mounted by udisks2.
info "Installing systemd mount unit…"
cp "${INSTALL_DIR}/media-ipod.mount" /etc/systemd/system/

# ── systemd services ─────────────────────────────────────────
info "Installing systemd services…"
cp "${INSTALL_DIR}/ipod-sync.service" /etc/systemd/system/
cp "${INSTALL_DIR}/ipod-sync-daemon.service" /etc/systemd/system/

# Patch paths and user in service files
sed -i "s|/opt/ipod-sync|${INSTALL_DIR}|g" /etc/systemd/system/ipod-sync.service
sed -i "s|/opt/ipod-sync|${INSTALL_DIR}|g" /etc/systemd/system/ipod-sync-daemon.service
sed -i "s|User=ipod-sync|User=${SERVICE_USER}|g" /etc/systemd/system/ipod-sync.service
sed -i "s|Group=ipod-sync|Group=${SERVICE_USER}|g" /etc/systemd/system/ipod-sync.service

systemctl daemon-reload
systemctl enable ipod-sync.service
systemctl enable ipod-sync-daemon.service

info "Starting services…"
systemctl restart ipod-sync.service
sleep 3
systemctl restart ipod-sync-daemon.service

# ── Done ─────────────────────────────────────────────────────
IP=$(hostname -I | awk '{print $1}')
WEB_URL="http://${IP}:8000"

echo
echo -e "${GREEN}╔══════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║       iPod Sync installed!               ║${NC}"
echo -e "${GREEN}╚══════════════════════════════════════════╝${NC}"
echo
echo -e "  Web UI:    ${YELLOW}${WEB_URL}${NC}"
echo -e "  Data dir:  ${DATA_DIR}"
echo -e "  Cache dir: ${CACHE_DIR}"
echo -e "  Mount:     ${MOUNT_POINT}"
echo
echo -e "  Service status:"
if systemctl is-active --quiet ipod-sync.service; then
    echo -e "    ${GREEN}✓${NC} ipod-sync (web UI)"
else
    echo -e "    ${RED}✗${NC} ipod-sync  ← check: journalctl -u ipod-sync"
fi
if systemctl is-active --quiet ipod-sync-daemon.service; then
    echo -e "    ${GREEN}✓${NC} ipod-sync-daemon (USB watcher)"
else
    echo -e "    ${RED}✗${NC} ipod-sync-daemon  ← check: journalctl -u ipod-sync-daemon"
fi
echo
echo -e "  ${YELLOW}Next steps:${NC}"
echo -e "    1. Open ${WEB_URL}"
echo -e "    2. Settings → enter your Jellyfin URL, API key, and User ID"
echo -e "    3. Settings → enter Pinepods URL + API key (if using Pinepods)"
echo -e "    4. Podcasts → add RSS feeds or import from Pinepods"
echo -e "    5. Plug in your iPod — sync starts automatically"
echo -e "       OR click 'Sync Now' in the web UI"
echo
echo -e "  ${YELLOW}Rockbox play-history sync (optional):${NC}"
echo -e "    On your iPod: Settings → General Settings → Playback → Last.fm Log → ON"
echo
echo -e "  Logs: journalctl -fu ipod-sync"
