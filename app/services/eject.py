"""
Safe iPod eject.

1. Refuses to eject while a sync is in progress.
2. Flushes kernel write buffers (sync).
3. Unmounts the device.
4. Optionally power-cycles the USB port via udisksctl for a clean disconnect.
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
    try:
        with open("/proc/mounts") as f:
            for line in f:
                if line.split()[1] == mount_point:
                    mounted = True
                    break
    except Exception:
        pass

    if not mounted:
        return "iPod is not mounted — already ejected?"

    # Flush write buffers
    log.info("Flushing write buffers…")
    _run(["sync"])

    # Try udisksctl first (friendlier, powers down the port)
    result = _run(["udisksctl", "unmount", "-b", _device_for_mount(mount_point)])
    if result.returncode == 0:
        log.info("Ejected via udisksctl")
        # Power off the drive
        _run(["udisksctl", "power-off", "-b", _device_for_mount(mount_point)])
        return "iPod ejected safely — you can unplug it"

    # Fall back to plain umount
    result = _run(["umount", mount_point])
    if result.returncode != 0:
        stderr = result.stderr.strip()
        if "target is busy" in stderr:
            raise EjectError(
                "Cannot eject — a file is still open on the iPod. "
                "Wait for the sync to finish."
            )
        raise EjectError(f"umount failed: {stderr or result.stdout}")

    log.info("Ejected via umount")
    return "iPod ejected safely — you can unplug it"


def _device_for_mount(mount_point: str) -> str:
    """Reverse-lookup the block device for a mount point from /proc/mounts."""
    try:
        with open("/proc/mounts") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2 and parts[1] == mount_point:
                    return parts[0]
    except Exception:
        pass
    return ""
