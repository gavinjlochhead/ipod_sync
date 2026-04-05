"""
Safe iPod eject.

1. Refuses to eject while a sync is in progress.
2. Flushes kernel write buffers (sync).
3. Unmounts the device via udisksctl (preferred) or sudo umount.
4. Powers off the USB drive via udisksctl for a clean disconnect.

The web app runs as the non-root `ipod-sync` user.  install.sh adds
sudoers rules that allow this user to call udisksctl and umount without
a password.
"""
from __future__ import annotations

import logging
import subprocess

log = logging.getLogger(__name__)


class EjectError(RuntimeError):
    pass


def _run(cmd: list[str], timeout: int = 15) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def eject(mount_point: str) -> str:
    """
    Safely unmount the iPod at *mount_point*.

    Returns a status message on success; raises EjectError on failure.
    """
    from pathlib import Path

    mp = Path(mount_point)
    if not mp.is_dir():
        raise EjectError(f"Mount point {mount_point!r} does not exist")

    # Check something is actually mounted there
    mounted = False
    device = ""
    try:
        with open("/proc/mounts") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2 and parts[1] == mount_point:
                    mounted = True
                    device = parts[0]
                    break
    except Exception:
        pass

    if not mounted:
        return "iPod is not mounted — already ejected?"

    # Flush write buffers (runs as root via PATH, doesn't need sudo)
    log.info("Flushing write buffers…")
    _run(["sync"])

    # Try udisksctl first — friendlier, powers down the USB port.
    # Installed sudoers rule covers: udisksctl unmount -b *
    if device:
        result = _run(["sudo", "udisksctl", "unmount", "-b", device])
        if result.returncode == 0:
            log.info("Ejected via udisksctl (%s)", device)
            _run(["sudo", "udisksctl", "power-off", "-b", device])
            return "iPod ejected safely — you can unplug it"

    # Fall back to sudo umount
    # Installed sudoers rule covers: umount <MOUNT_POINT>
    result = _run(["sudo", "umount", mount_point])
    if result.returncode != 0:
        stderr = result.stderr.strip()
        if "target is busy" in stderr or "device is busy" in stderr:
            raise EjectError(
                "Cannot eject — a file is still open on the iPod. "
                "Wait for the sync to finish."
            )
        raise EjectError(f"umount failed: {stderr or result.stdout}")

    log.info("Ejected via sudo umount")
    return "iPod ejected safely — you can unplug it"
