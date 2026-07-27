"""
Manually (re)mount the iPod.

Companion to eject.py — lets a "Mount" button/API call bring the iPod back
online after it's been safely ejected, without needing to unplug/replug it.
udisks2 auto-mounts on plug-in already; this covers the case where the user
(or a physical button) wants to remount without a fresh USB connect event.

The web app runs as the non-root `ipod-sync` user.  install.sh adds a
sudoers rule that allows this user to call `udisksctl mount` without a
password.
"""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)


class MountError(RuntimeError):
    pass


def _run(cmd: list[str], timeout: int = 15) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def mount(label: str = "IPOD") -> str:
    """
    Mount the block device with filesystem label *label* via udisksctl.

    Returns a status message on success; raises MountError on failure.
    """
    link = Path("/dev/disk/by-label") / label
    if not link.exists():
        raise MountError(
            f"No device found with label {label!r} — is the iPod plugged in?"
        )
    device = str(link.resolve())

    try:
        with open("/proc/mounts") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2 and parts[0] == device:
                    return f"iPod already mounted at {parts[1]}"
    except Exception:
        pass

    result = _run(["sudo", "udisksctl", "mount", "-b", device])
    if result.returncode != 0:
        stderr = result.stderr.strip() or result.stdout.strip()
        raise MountError(f"mount failed: {stderr}")

    log.info("Mounted %s via udisksctl", device)
    return result.stdout.strip() or "iPod mounted"
