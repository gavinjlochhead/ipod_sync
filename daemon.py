#!/usr/bin/env python3
"""
iPod detection daemon.

Uses pyudev to monitor USB block devices.  When a device with the
configured filesystem label (default: IPOD) is added and mounted,
it notifies the web app to start a sync via the local API.

Run as a systemd service alongside the web app.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import time

import httpx
import pyudev

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("ipod-daemon")

# Web app base URL (local, same machine)
API_BASE = os.environ.get("IPOD_SYNC_API", "http://127.0.0.1:8000")
IPOD_LABEL = os.environ.get("IPOD_LABEL", "IPOD")


def get_device_label(device_node: str) -> str | None:
    """Return the filesystem label for a block device node, or None."""
    try:
        out = subprocess.check_output(
            ["blkid", "-s", "LABEL", "-o", "value", device_node],
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
        return out.decode().strip()
    except Exception:
        return None


def trigger_sync(label: str, mount: str | None) -> None:
    """POST to the web app to kick off a sync."""
    try:
        r = httpx.post(f"{API_BASE}/api/sync/start", timeout=10)
        if r.status_code == 200:
            log.info("Sync triggered (device label=%s, mount=%s)", label, mount)
        else:
            log.warning("Sync trigger returned %s: %s", r.status_code, r.text)
    except Exception as exc:
        log.error("Failed to trigger sync: %s", exc)


def is_sync_on_connect_enabled() -> bool:
    """Check the app setting before auto-triggering."""
    try:
        r = httpx.get(f"{API_BASE}/api/settings", timeout=5)
        if r.status_code == 200:
            return r.json().get("sync_on_connect", True)
    except Exception:
        pass
    return True


def wait_for_mount(device_node: str, timeout: int = 30) -> str | None:
    """
    Poll /proc/mounts until the device appears as mounted.
    Returns the mount point or None on timeout.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with open("/proc/mounts") as f:
                for line in f:
                    parts = line.split()
                    if len(parts) >= 2 and parts[0] == device_node:
                        return parts[1]
        except Exception:
            pass
        time.sleep(1)
    return None


def wait_for_api(timeout: int = 60) -> None:
    """Block until the web app is up and responding."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = httpx.get(f"{API_BASE}/api/sync/status", timeout=3)
            if r.status_code == 200:
                log.info("Web app is up at %s", API_BASE)
                return
        except Exception:
            pass
        time.sleep(2)
    log.warning("Web app did not respond within %ds — proceeding anyway", timeout)


def main() -> None:
    log.info("iPod sync daemon starting (watching for label=%s)", IPOD_LABEL)
    wait_for_api()

    context = pyudev.Context()
    monitor = pyudev.Monitor.from_netlink(context)
    monitor.filter_by("block")

    log.info("Watching for USB block devices…")

    for device in iter(monitor.poll, None):
        if device.action != "add":
            continue

        # Only care about partitions (fstype usually on partition, not whole disk)
        device_node = device.device_node
        if not device_node:
            continue

        label = get_device_label(device_node)
        if not label:
            # Try the device itself if no partition label found quickly
            time.sleep(2)
            label = get_device_label(device_node)

        if not label or label.upper() != IPOD_LABEL.upper():
            continue

        log.info("Detected device with label=%s at %s", label, device_node)

        # Wait for automount
        mount = wait_for_mount(device_node, timeout=20)
        if not mount:
            log.warning("Device mounted nowhere after 20s — triggering anyway")

        if is_sync_on_connect_enabled():
            # Small delay to let mount settle
            time.sleep(2)
            trigger_sync(label, mount)
        else:
            log.info("sync_on_connect is disabled — skipping auto-sync")


if __name__ == "__main__":
    main()
