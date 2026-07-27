"""
Optional Raspberry Pi GPIO control: physical buttons + status LEDs.

Buttons (momentary, wired to GND, internal pull-up):
  - Mount button   -> (re)mount the iPod
  - Unmount button -> safely eject the iPod
  - Sync button    -> start a sync

LEDs:
  - Mounted LED    -> on while the iPod is mounted
  - Removable LED  -> on while the iPod is NOT mounted (safe to unplug)
  - Syncing LED    -> blinks while a sync is in progress

Every pin is independently optional — wire only the buttons/LEDs you have.
If gpiozero isn't installed, or no pins are configured, this module is a
no-op. Uses BCM pin numbering throughout.
"""
from __future__ import annotations

import asyncio
import logging

from app.services import eject as eject_svc
from app.services import mount as mount_svc
from app.services import sync as sync_svc
from app.services.ipod import IPodManager

log = logging.getLogger(__name__)

_button_mount = None
_button_unmount = None
_button_sync = None
_led_mounted = None
_led_removable = None
_led_syncing = None

_loop: asyncio.AbstractEventLoop | None = None
_poll_task: asyncio.Task | None = None
_ipod_label = "IPOD"
_mount_point = ""
_session_factory = None


def configure(
    *,
    enabled: bool,
    mount_pin: int | None,
    unmount_pin: int | None,
    sync_pin: int | None,
    led_mounted_pin: int | None,
    led_removable_pin: int | None,
    led_syncing_pin: int | None,
    mount_point: str,
    ipod_label: str,
    session_factory,
) -> None:
    """(Re)configure GPIO buttons/LEDs from saved settings."""
    global _mount_point, _ipod_label, _session_factory, _loop, _poll_task

    _teardown()

    _mount_point = mount_point
    _ipod_label = ipod_label or "IPOD"
    _session_factory = session_factory

    if not enabled:
        return

    try:
        _loop = asyncio.get_running_loop()
    except RuntimeError:
        _loop = None
        log.warning("GPIO configure() called outside an event loop — buttons disabled")
        return

    try:
        from gpiozero import Button, PWMLED
    except ImportError:
        log.info("gpiozero not installed — GPIO controls disabled")
        return

    global _button_mount, _button_unmount, _button_sync
    global _led_mounted, _led_removable, _led_syncing

    try:
        if mount_pin is not None:
            _button_mount = Button(mount_pin, bounce_time=0.2, pull_up=True)
            _button_mount.when_pressed = _on_mount_pressed
        if unmount_pin is not None:
            _button_unmount = Button(unmount_pin, bounce_time=0.2, pull_up=True)
            _button_unmount.when_pressed = _on_unmount_pressed
        if sync_pin is not None:
            _button_sync = Button(sync_pin, bounce_time=0.2, pull_up=True)
            _button_sync.when_pressed = _on_sync_pressed

        if led_mounted_pin is not None:
            _led_mounted = PWMLED(led_mounted_pin)
        if led_removable_pin is not None:
            _led_removable = PWMLED(led_removable_pin)
        if led_syncing_pin is not None:
            _led_syncing = PWMLED(led_syncing_pin)
    except Exception as exc:
        log.warning("GPIO setup failed: %s", exc)
        _teardown()
        return

    log.info("GPIO configured — buttons/LEDs active")
    _poll_task = _loop.create_task(_poll_status())


def _teardown() -> None:
    global _button_mount, _button_unmount, _button_sync
    global _led_mounted, _led_removable, _led_syncing, _poll_task

    if _poll_task is not None:
        _poll_task.cancel()
        _poll_task = None

    for dev in (_button_mount, _button_unmount, _button_sync,
                _led_mounted, _led_removable, _led_syncing):
        if dev is not None:
            try:
                dev.close()
            except Exception:
                pass

    _button_mount = _button_unmount = _button_sync = None
    _led_mounted = _led_removable = _led_syncing = None


async def _poll_status(interval: float = 1.0) -> None:
    blink_on = False
    while True:
        try:
            mount_pt = _mount_point or IPodManager.find_ipod_mount(_ipod_label)
            mounted = bool(mount_pt) and IPodManager(mount_pt).is_mounted()
            syncing = sync_svc.get_status().get("running", False)

            if _led_mounted is not None:
                _led_mounted.value = 1.0 if mounted else 0.0
            if _led_removable is not None:
                _led_removable.value = 0.0 if mounted else 1.0
            if _led_syncing is not None:
                if syncing:
                    blink_on = not blink_on
                    _led_syncing.value = 1.0 if blink_on else 0.0
                else:
                    blink_on = False
                    _led_syncing.value = 0.0
        except Exception as exc:
            log.debug("GPIO poll error: %s", exc)
        await asyncio.sleep(interval)


# ---------------------------------------------------------------------------
# Button callbacks — gpiozero invokes these on its own thread, so hop onto
# the asyncio event loop to run the (async) action.
# ---------------------------------------------------------------------------

def _schedule(coro) -> None:
    if _loop is None:
        log.warning("GPIO button pressed but no event loop is registered")
        return
    asyncio.run_coroutine_threadsafe(coro, _loop)


def _on_mount_pressed() -> None:
    log.info("GPIO: mount button pressed")
    _schedule(_do_mount())


def _on_unmount_pressed() -> None:
    log.info("GPIO: unmount button pressed")
    _schedule(_do_unmount())


def _on_sync_pressed() -> None:
    log.info("GPIO: sync button pressed")
    _schedule(_do_sync())


async def _do_mount() -> None:
    try:
        message = await asyncio.to_thread(mount_svc.mount, _ipod_label)
        log.info("GPIO mount: %s", message)
    except Exception as exc:
        log.warning("GPIO mount failed: %s", exc)


async def _do_unmount() -> None:
    if sync_svc.get_status().get("running"):
        log.warning("GPIO unmount: sync in progress, refusing to eject")
        return
    mount_pt = _mount_point or IPodManager.find_ipod_mount(_ipod_label)
    if not mount_pt:
        log.warning("GPIO unmount: iPod mount point not found")
        return
    try:
        message = await asyncio.to_thread(eject_svc.eject, mount_pt)
        log.info("GPIO unmount: %s", message)
    except Exception as exc:
        log.warning("GPIO unmount failed: %s", exc)


async def _do_sync() -> None:
    if sync_svc.get_status().get("running"):
        log.info("GPIO sync: already running")
        return
    if _session_factory is None:
        log.warning("GPIO sync: no session factory configured")
        return
    asyncio.create_task(sync_svc.run_sync(_session_factory))
