"""
Optional MQTT publisher for Home Assistant integration.

Publishes iPod Sync status to an MQTT broker so Home Assistant can
display it via MQTT sensors.  All publishing is fire-and-forget;
failures are logged but never raise.

Topics published (all under a configurable prefix, default "ipod_sync"):

  ipod_sync/status              → "idle" | "syncing" | "error"
  ipod_sync/last_sync_time      → ISO-8601 UTC string or ""
  ipod_sync/last_sync_status    → "success" | "error" | ""
  ipod_sync/music_on_device     → integer
  ipod_sync/podcasts_on_device  → integer
  ipod_sync/ipod_connected      → "true" | "false"
  ipod_sync/ipod_free_gb        → float string or ""
  ipod_sync/message             → last status message string

Topic subscribed (inbound commands):

  ipod_sync/eject/set           → any payload triggers a safe eject

Home Assistant auto-discovery is also published if enabled, creating
sensor and button entities automatically.
"""
from __future__ import annotations

import json
import logging
import threading
from typing import Any

log = logging.getLogger(__name__)

# Module-level client singleton (None when MQTT is not configured)
_client = None
_lock = threading.Lock()
_prefix = "ipod_sync"
_ha_discovery = True
# Cached local API base — set during configure() so the callback can reach it
_api_base = "http://127.0.0.1:8000"

# Shared HA device block so all entities appear under one device in HA
_HA_DEVICE = {
    "identifiers": ["ipod_sync_rpi"],
    "name": "iPod Sync",
    "model": "Raspberry Pi",
    "manufacturer": "ipod-sync",
}


def configure(
    host: str,
    port: int = 1883,
    username: str | None = None,
    password: str | None = None,
    prefix: str = "ipod_sync",
    ha_discovery: bool = True,
    api_base: str = "http://127.0.0.1:8000",
) -> None:
    global _client, _prefix, _ha_discovery, _api_base
    _prefix = prefix.rstrip("/")
    _ha_discovery = ha_discovery
    _api_base = api_base.rstrip("/")

    try:
        import paho.mqtt.client as mqtt  # type: ignore

        with _lock:
            if _client:
                try:
                    _client.disconnect()
                except Exception:
                    pass

            c = mqtt.Client(client_id="ipod-sync", clean_session=True)
            if username:
                c.username_pw_set(username, password)

            c.on_connect = _on_connect
            c.on_message = _on_message

            c.connect_async(host, port, keepalive=60)
            c.loop_start()
            _client = c
            log.info("MQTT configured → %s:%s prefix=%s", host, port, prefix)
    except ImportError:
        log.warning("paho-mqtt not installed — MQTT disabled")
    except Exception as exc:
        log.warning("MQTT connect failed: %s", exc)


# ---------------------------------------------------------------------------
# Callbacks
# ---------------------------------------------------------------------------

def _on_connect(client, userdata, flags, rc) -> None:
    if rc == 0:
        log.info("MQTT connected")
        # Subscribe to command topics
        eject_topic = f"{_prefix}/eject/set"
        client.subscribe(eject_topic)
        log.info("Subscribed to MQTT command topic: %s", eject_topic)
    else:
        log.warning("MQTT connection failed, rc=%s", rc)


def _on_message(client, userdata, msg) -> None:
    """Handle inbound MQTT command messages in a worker thread."""
    topic = msg.topic
    eject_topic = f"{_prefix}/eject/set"

    if topic == eject_topic:
        log.info("Received eject command via MQTT")
        t = threading.Thread(target=_handle_eject, daemon=True)
        t.start()
    else:
        log.debug("Unhandled MQTT topic: %s", topic)


def _handle_eject() -> None:
    """
    Called from the paho callback thread.
    POSTs to the local REST API so all the eject logic (sync-in-progress
    check, mount detection, umount) lives in one place.
    """
    import httpx

    try:
        r = httpx.post(f"{_api_base}/api/eject", timeout=30)
        data = r.json()
        if r.status_code == 200 and data.get("ok"):
            log.info("MQTT eject: %s", data.get("message"))
            publish("eject/state", "ejected", retain=False)
        else:
            detail = data.get("detail") or data.get("message") or str(r.status_code)
            log.warning("MQTT eject failed: %s", detail)
            publish("eject/state", f"error: {detail}", retain=False)
    except Exception as exc:
        log.error("MQTT eject handler error: %s", exc)
        publish("eject/state", f"error: {exc}", retain=False)


# ---------------------------------------------------------------------------
# Publish helpers
# ---------------------------------------------------------------------------

def publish(topic_suffix: str, payload: Any, retain: bool = True) -> None:
    with _lock:
        c = _client
    if c is None:
        return
    try:
        c.publish(f"{_prefix}/{topic_suffix}", str(payload), retain=retain)
    except Exception as exc:
        log.debug("MQTT publish error: %s", exc)


def publish_status(status: dict) -> None:
    publish("status", "syncing" if status.get("running") else "idle")
    publish("message", status.get("message", ""))


def publish_sync_result(log_entry: dict) -> None:
    publish("last_sync_time", log_entry.get("finished_at") or "")
    publish("last_sync_status", log_entry.get("status") or "")
    publish("music_on_device", log_entry.get("music_added", 0))


def publish_device_info(
    connected: bool,
    free_gb: float | None = None,
    total_gb: float | None = None,
    music_on_device: int = 0,
    podcasts_on_device: int = 0,
) -> None:
    publish("ipod_connected", "true" if connected else "false")
    publish("ipod_free_gb", str(free_gb) if free_gb is not None else "")
    publish("ipod_total_gb", str(total_gb) if total_gb is not None else "")
    publish("music_on_device", music_on_device)
    publish("podcasts_on_device", podcasts_on_device)


# ---------------------------------------------------------------------------
# Home Assistant discovery
# ---------------------------------------------------------------------------

def publish_ha_discovery() -> None:
    """
    Publish MQTT discovery payloads so HA auto-creates all entities
    grouped under a single 'iPod Sync' device.
    """
    if not _ha_discovery:
        return

    with _lock:
        c = _client
    if not c:
        return

    # Sensors
    sensors = [
        ("status",             "iPod Sync Status",         None,  "mdi:sync"),
        ("last_sync_time",     "iPod Last Sync Time",       None,  "mdi:clock-check"),
        ("last_sync_status",   "iPod Last Sync Result",     None,  "mdi:check-circle"),
        ("music_on_device",    "iPod Music Tracks",         None,  "mdi:music"),
        ("podcasts_on_device", "iPod Podcast Episodes",     None,  "mdi:podcast"),
        ("ipod_connected",     "iPod Connected",            None,  "mdi:usb"),
        ("ipod_free_gb",       "iPod Free Space",           "GB",  "mdi:harddisk"),
        ("message",            "iPod Sync Message",         None,  "mdi:information"),
    ]

    for suffix, name, unit, icon in sensors:
        unique_id = f"ipod_sync_{suffix}"
        payload: dict = {
            "name": name,
            "unique_id": unique_id,
            "state_topic": f"{_prefix}/{suffix}",
            "icon": icon,
            "device": _HA_DEVICE,
        }
        if unit:
            payload["unit_of_measurement"] = unit
        _discovery_publish(f"homeassistant/sensor/{unique_id}/config", payload)

    # Eject button
    _discovery_publish(
        f"homeassistant/button/ipod_sync_eject/config",
        {
            "name": "Eject iPod",
            "unique_id": "ipod_sync_eject",
            "command_topic": f"{_prefix}/eject/set",
            "payload_press": "PRESS",
            "icon": "mdi:eject",
            "device": _HA_DEVICE,
            # Availability: only show button as available when iPod is connected
            "availability_topic": f"{_prefix}/ipod_connected",
            "payload_available": "true",
            "payload_not_available": "false",
        },
    )

    log.info("Published HA MQTT discovery payloads (sensors + eject button)")


def _discovery_publish(topic: str, payload: dict) -> None:
    with _lock:
        c = _client
    if c:
        try:
            c.publish(topic, json.dumps(payload), retain=True)
        except Exception as exc:
            log.debug("HA discovery publish error: %s", exc)


def is_configured() -> bool:
    with _lock:
        return _client is not None
