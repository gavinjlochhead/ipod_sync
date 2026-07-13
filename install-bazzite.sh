#!/usr/bin/env bash
# ============================================================
# iPod Sync — Bazzite / SteamOS-style (rpm-ostree) Installer
# ============================================================
# For running iPod Sync on a Bazzite handheld (e.g. ROG Ally) instead
# of a Raspberry Pi. Everything is identical to install.sh except how
# system packages are provided, since Bazzite is an immutable
# rpm-ostree image and has no apt-get.
#
# Run as root:  sudo bash install-bazzite.sh
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

[[ $EUID -ne 0 ]] && error "Run as root: sudo bash install-bazzite.sh"

command -v rpm-ostree >/dev/null 2>&1 || \
    error "rpm-ostree not found — this script is for Bazzite/Fedora Atomic. Use install.sh on Raspberry Pi OS."

# ── Dependencies ────────────────────────────────────────────
# Bazzite's base image already ships rsync, udisks2, util-linux (blkid),
# udev and python3 as core OS tooling. Only layer packages that are
# actually missing, since rpm-ostree installs are heavier (some need a
# reboot to fully apply) than apt-get on Debian.
info "Checking for required system tools…"
REQUIRED_BINS=(rsync udisksctl blkid udevadm python3)
MISSING_PKGS=()

for bin in "${REQUIRED_BINS[@]}"; do
    if ! command -v "$bin" >/dev/null 2>&1; then
        case "$bin" in
            rsync)      MISSING_PKGS+=("rsync") ;;
            udisksctl)  MISSING_PKGS+=("udisks2") ;;
            blkid)      MISSING_PKGS+=("util-linux") ;;
            udevadm)    MISSING_PKGS+=("systemd-udev") ;;
            python3)    MISSING_PKGS+=("python3") ;;
        esac
    fi
done

# python3-venv/pip are sometimes trimmed from atomic base images even
# when the python3 binary itself is present.
"${PYTHON}" -m venv --help >/dev/null 2>&1 || MISSING_PKGS+=("python3-pip")

if [[ ${#MISSING_PKGS[@]} -gt 0 ]]; then
    info "Layering missing packages via rpm-ostree: ${MISSING_PKGS[*]}"
    rpm-ostree install --idempotent --apply-live "${MISSING_PKGS[@]}"
    warn "If any tool above still isn't on PATH, reboot the Ally once and re-run this script."
else
    info "All required system tools already present — nothing to layer."
fi

# ── User & directories ───────────────────────────────────────
info "Creating service user '${SERVICE_USER}'…"
if ! id "${SERVICE_USER}" &>/dev/null; then
    useradd --system --no-create-home --shell /usr/sbin/nologin \
        --groups plugdev "${SERVICE_USER}" || true
fi

# plugdev  → udisks2 mount/unmount without root
# disk     → blkid can read device labels
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

# ── sudoers rules ───────────────────────────────────────────
# Allow the service user to unmount and run blkid without a password.
# These are tightly scoped — no general sudo access is granted.
info "Installing sudoers rules…"
SUDOERS_FILE="/etc/sudoers.d/ipod-sync"
tee "${SUDOERS_FILE}" > /dev/null <<EOF
# ipod-sync: allow safe eject and device label detection
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
# /etc is writable on ostree systems (only /usr is read-only), so this
# works exactly the same as on Raspberry Pi OS.
info "Installing udev rules…"
cp "${INSTALL_DIR}/90-ipod.rules" /etc/udev/rules.d/
udevadm control --reload-rules
udevadm trigger

# ── systemd mount unit (optional — udisks2 usually auto-mounts) ───
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
echo -e "       (use your home server's Tailscale IP/hostname if syncing away from home)"
echo -e "    3. Settings → enter Pinepods URL + API key (if using Pinepods)"
echo -e "    4. Podcasts → add RSS feeds or import from Pinepods"
echo -e "    5. Plug in your iPod — sync starts automatically"
echo -e "       OR click 'Sync Now' in the web UI"
echo
echo -e "  ${YELLOW}Rockbox play-history sync (optional):${NC}"
echo -e "    On your iPod: Settings → General Settings → Playback → Last.fm Log → ON"
echo
echo -e "  Logs: journalctl -fu ipod-sync"
echo
if [[ ${#MISSING_PKGS[@]} -gt 0 ]]; then
    echo -e "  ${YELLOW}Note:${NC} some packages were layered via rpm-ostree (${MISSING_PKGS[*]})."
    echo -e "  If iPod detection or mounting misbehaves, reboot the Ally once so the"
    echo -e "  layered packages are fully active, then re-run 'systemctl restart ipod-sync-daemon'."
fi
