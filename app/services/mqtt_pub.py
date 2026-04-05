"""
Optional MQTT publisher for Home Assistant integration.

Publishes iPod Sync status to an MQTT broker so Home Assistant can
display it via MQTT sensors.  All publishing is fire-and-forget;
failures are logged but never raise.

Topics published (all under a configurable prefix, default "ipod_sync"):

  ipod_sync/status          → "idle" | "syncing" | "error"
  ipod_sync/last_sync_time  → ISO-8601 UTC string or ""
  ipod_sync/last_sync_status → "success" | "error" | ""
  ipod_sync/music_on_device → integer
  ipod_sync/podcasts_on_device → integer
  ipod_sync/ipod_connected  → "true" | "false"
  ipod_sync/ipod_free_gb    → float string or ""
  ipod_sync/message         → last status message string

Home Assistant auto-discovery is also published if enabled, creating
sensor entities automatically.
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


def configure(
    host: str,
    port: int = 1883,
    username: str | None = None,
    password: str | None = None,
    prefix: str = "ipod_sync",
    ha_discovery: bool = True,
) -> None:
    global _client, _prefix, _ha_discovery
    _prefix = prefix.rstrip("/")
    _ha_discovery = ha_discovery

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
            c.connect_async(host, port, keepalive=60)
            c.loop_start()
            _client = c
            log.info("MQTT configured → %s:%s prefix=%s", host, port, prefix)
    except ImportError:
        log.warning("paho-mqtt not installed — MQTT disabled")
    except Exception as exc:
        log.warning("MQTT connect failed: %s", exc)


def publish(topic_suffix: str, payload: Any, retain: bool = True) -> None:
    with _lock:
        c = _client
    if c is None:
        return
    try:
        full_topic = f"{_prefix}/{topic_suffix}"
        c.publish(full_topic, str(payload), retain=retain)
    except Exception as exc:
        log.debug("MQTT publish error: %s", exc)


def publish_status(status: dict) -> None:
    """Publish the full sync status dict to individual MQTT topics."""
    publish("status", "syncing" if status.get("running") else "idle")
    publish("message", status.get("message", ""))


def publish_sync_result(log_entry: dict) -> None:
    """Call after a sync completes with the SyncLog dict."""
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


def publish_ha_discovery() -> None:
    """
    Publish MQTT discovery payloads so HA auto-creates sensor entities.
    Call once after connecting.
    """
    if not _ha_discovery:
        return

    sensors = [
        ("status",             "iPod Sync Status",          None,    "mdi:sync"),
        ("last_sync_time",     "iPod Last Sync Time",        None,    "mdi:clock-check"),
        ("last_sync_status",   "iPod Last Sync Result",      None,    "mdi:check-circle"),
        ("music_on_device",    "iPod Music Tracks",          None,    "mdi:music"),
        ("podcasts_on_device", "iPod Podcast Episodes",      None,    "mdi:podcast"),
        ("ipod_connected",     "iPod Connected",             None,    "mdi:usb"),
        ("ipod_free_gb",       "iPod Free Space",            "GB",    "mdi:harddisk"),
        ("message",            "iPod Sync Message",          None,    "mdi:information"),
    ]

    for suffix, name, unit, icon in sensors:
        unique_id = f"ipod_sync_{suffix}"
        payload = {
            "name": name,
            "unique_id": unique_id,
            "state_topic": f"{_prefix}/{suffix}",
            "icon": icon,
        }
        if unit:
            payload["unit_of_measurement"] = unit
        disc_topic = f"homeassistant/sensor/{unique_id}/config"
        with _lock:
            c = _client
        if c:
            try:
                c.publish(disc_topic, json.dumps(payload), retain=True)
            except Exception as exc:
                log.debug("HA discovery publish error: %s", exc)

    log.info("Published HA MQTT discovery payloads")


def is_configured() -> bool:
    with _lock:
        return _client is not None
