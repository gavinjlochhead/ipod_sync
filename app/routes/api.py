"""
REST API routes used by the frontend (AJAX calls).
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession

from app import config as cfg
from app.database import get_db, AsyncSessionLocal
from app.models import PodcastSubscription, PodcastEpisode, SyncLog, MusicTrack
from app.services import sync as sync_svc
from app.services import rss as rss_svc
from app.services.jellyfin import JellyfinClient
from app.services.pinepods import PinepodsClient
from app.services.ipod import IPodManager
from app.services.eject import eject, EjectError
from app.services import mqtt_pub
from app.services import scrobbler as scrobbler_svc

log = logging.getLogger(__name__)
router = APIRouter()


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------

@router.post("/sync/start")
async def start_sync():
    """Trigger a sync in the background."""
    if sync_svc.get_status()["running"]:
        return {"ok": False, "message": "Sync already running"}
    asyncio.create_task(sync_svc.run_sync(AsyncSessionLocal))
    return {"ok": True, "message": "Sync started"}


@router.get("/sync/status")
async def sync_status():
    return sync_svc.get_status()


@router.post("/sync/scrobbler")
async def sync_scrobbler_only(db: AsyncSession = Depends(get_db)):
    """Read .scrobbler.log from the iPod and push play history to Jellyfin."""
    s = await cfg.get_all(db)
    mount = s.get(cfg.IPOD_MOUNT) or IPodManager.find_ipod_mount(
        s.get(cfg.IPOD_LABEL, "IPOD") or "IPOD"
    )
    if not mount:
        raise HTTPException(404, "iPod not found")
    entries = scrobbler_svc.parse_scrobbler_log(mount)
    if not entries:
        return {"ok": True, "matched": 0, "message": "No scrobbler log found"}
    from app.database import AsyncSessionLocal
    matched, failed = await scrobbler_svc.sync_scrobbles_to_jellyfin(entries, AsyncSessionLocal)
    scrobbler_svc.archive_scrobbler_log(mount)
    return {"ok": True, "matched": matched, "failed": failed,
            "total": len([e for e in entries if e.was_played])}


@router.post("/eject")
async def eject_ipod(db: AsyncSession = Depends(get_db)):
    """Safely unmount the iPod. Refuses if a sync is in progress."""
    if sync_svc.get_status()["running"]:
        raise HTTPException(409, "Sync is running — wait for it to finish before ejecting")
    s = await cfg.get_all(db)
    mount = s.get(cfg.IPOD_MOUNT) or IPodManager.find_ipod_mount(
        s.get(cfg.IPOD_LABEL, "IPOD") or "IPOD"
    )
    if not mount:
        raise HTTPException(404, "iPod mount point not found")
    try:
        import asyncio
        message = await asyncio.to_thread(eject, mount)
        return {"ok": True, "message": message}
    except EjectError as exc:
        raise HTTPException(500, str(exc))


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

class SettingsPayload(BaseModel):
    jellyfin_url: str = ""
    jellyfin_api_key: str = ""
    jellyfin_user_id: str = ""
    jellyfin_library_id: str = ""
    pinepods_url: str = ""
    pinepods_api_key: str = ""
    ipod_mount: str = ""
    ipod_label: str = "IPOD"
    cache_dir: str = "/var/cache/ipod-sync/podcasts"
    music_remove_deleted: bool = False
    sync_on_connect: bool = True
    scrobble_to_jellyfin: bool = True
    # MQTT
    mqtt_host: str = ""
    mqtt_port: int = 1883
    mqtt_username: str = ""
    mqtt_password: str = ""
    mqtt_prefix: str = "ipod_sync"
    mqtt_ha_discovery: bool = True


@router.get("/settings")
async def get_settings(db: AsyncSession = Depends(get_db)):
    s = await cfg.get_all(db)
    return {
        "jellyfin_url": s.get(cfg.JELLYFIN_URL, ""),
        "jellyfin_api_key": s.get(cfg.JELLYFIN_API_KEY, ""),
        "jellyfin_user_id": s.get(cfg.JELLYFIN_USER_ID, ""),
        "jellyfin_library_id": s.get(cfg.JELLYFIN_LIBRARY_ID, ""),
        "pinepods_url": s.get(cfg.PINEPODS_URL, ""),
        "pinepods_api_key": s.get(cfg.PINEPODS_API_KEY, ""),
        "ipod_mount": s.get(cfg.IPOD_MOUNT, ""),
        "ipod_label": s.get(cfg.IPOD_LABEL, "IPOD"),
        "cache_dir": s.get(cfg.CACHE_DIR, "/var/cache/ipod-sync/podcasts"),
        "music_remove_deleted": s.get(cfg.MUSIC_REMOVE_DELETED, "false") == "true",
        "sync_on_connect": s.get(cfg.SYNC_ON_CONNECT, "true") == "true",
        "scrobble_to_jellyfin": s.get(cfg.SCROBBLE_TO_JELLYFIN, "true") == "true",
        "mqtt_host": s.get(cfg.MQTT_HOST, ""),
        "mqtt_port": int(s.get(cfg.MQTT_PORT, "1883") or "1883"),
        "mqtt_username": s.get(cfg.MQTT_USERNAME, ""),
        "mqtt_password": s.get(cfg.MQTT_PASSWORD, ""),
        "mqtt_prefix": s.get(cfg.MQTT_PREFIX, "ipod_sync"),
        "mqtt_ha_discovery": s.get(cfg.MQTT_HA_DISCOVERY, "true") == "true",
    }


@router.post("/settings")
async def save_settings(payload: SettingsPayload, db: AsyncSession = Depends(get_db)):
    await cfg.set_many(db, {
        cfg.JELLYFIN_URL: payload.jellyfin_url,
        cfg.JELLYFIN_API_KEY: payload.jellyfin_api_key,
        cfg.JELLYFIN_USER_ID: payload.jellyfin_user_id,
        cfg.JELLYFIN_LIBRARY_ID: payload.jellyfin_library_id,
        cfg.PINEPODS_URL: payload.pinepods_url,
        cfg.PINEPODS_API_KEY: payload.pinepods_api_key,
        cfg.IPOD_MOUNT: payload.ipod_mount,
        cfg.IPOD_LABEL: payload.ipod_label,
        cfg.CACHE_DIR: payload.cache_dir,
        cfg.MUSIC_REMOVE_DELETED: "true" if payload.music_remove_deleted else "false",
        cfg.SYNC_ON_CONNECT: "true" if payload.sync_on_connect else "false",
        cfg.SCROBBLE_TO_JELLYFIN: "true" if payload.scrobble_to_jellyfin else "false",
        cfg.MQTT_HOST: payload.mqtt_host,
        cfg.MQTT_PORT: str(payload.mqtt_port),
        cfg.MQTT_USERNAME: payload.mqtt_username,
        cfg.MQTT_PASSWORD: payload.mqtt_password,
        cfg.MQTT_PREFIX: payload.mqtt_prefix or "ipod_sync",
        cfg.MQTT_HA_DISCOVERY: "true" if payload.mqtt_ha_discovery else "false",
    })
    # Re-configure MQTT if host is provided
    if payload.mqtt_host:
        mqtt_pub.configure(
            host=payload.mqtt_host,
            port=payload.mqtt_port,
            username=payload.mqtt_username or None,
            password=payload.mqtt_password or None,
            prefix=payload.mqtt_prefix or "ipod_sync",
            ha_discovery=payload.mqtt_ha_discovery,
            api_base="http://127.0.0.1:8000",
        )
        if payload.mqtt_ha_discovery:
            mqtt_pub.publish_ha_discovery()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Home Assistant status endpoint
# ---------------------------------------------------------------------------

@router.get("/ha/status")
async def ha_status(db: AsyncSession = Depends(get_db)):
    """
    Single JSON endpoint for Home Assistant REST sensors.

    Add to configuration.yaml:
      sensor:
        - platform: rest
          resource: http://<pi-ip>:8000/api/ha/status
          name: iPod Sync
          json_attributes:
            - last_sync_time
            - last_sync_status
            - music_on_device
            - podcasts_on_device
            - ipod_free_gb
            - ipod_total_gb
            - message
          value_template: "{{ value_json.state }}"
    """
    from sqlalchemy import select
    from app.models import SyncLog, MusicTrack, PodcastEpisode

    status = sync_svc.get_status()

    # Last sync log
    last_log = await db.scalar(
        select(SyncLog).order_by(SyncLog.started_at.desc()).limit(1)
    )

    # Device info
    s = await cfg.get_all(db)
    mount = s.get(cfg.IPOD_MOUNT) or IPodManager.find_ipod_mount(
        s.get(cfg.IPOD_LABEL, "IPOD") or "IPOD"
    )
    ipod_connected = False
    free_gb = None
    total_gb = None
    if mount:
        ipod = IPodManager(mount)
        if ipod.is_mounted():
            ipod_connected = True
            free_gb = round(ipod.free_bytes() / 1e9, 2)
            total_gb = round(ipod.total_bytes() / 1e9, 2)

    music_on_device = await db.scalar(
        select(MusicTrack).where(MusicTrack.on_device == True)  # noqa: E712
    )
    podcasts_on_device = await db.scalar(
        select(PodcastEpisode).where(PodcastEpisode.on_device == True)  # noqa: E712
    )

    from sqlalchemy import func
    music_count = (await db.execute(
        select(func.count()).select_from(MusicTrack).where(MusicTrack.on_device == True)  # noqa: E712
    )).scalar() or 0
    podcast_count = (await db.execute(
        select(func.count()).select_from(PodcastEpisode).where(PodcastEpisode.on_device == True)  # noqa: E712
    )).scalar() or 0

    return {
        "state": "syncing" if status["running"] else (
            last_log.status if last_log else "idle"
        ),
        "last_sync_time": last_log.finished_at.isoformat() if last_log and last_log.finished_at else None,
        "last_sync_status": last_log.status if last_log else None,
        "music_on_device": music_count,
        "podcasts_on_device": podcast_count,
        "ipod_connected": ipod_connected,
        "ipod_free_gb": free_gb,
        "ipod_total_gb": total_gb,
        "message": status["message"],
    }


# ---------------------------------------------------------------------------
# Connection tests
# ---------------------------------------------------------------------------

@router.post("/test/jellyfin")
async def test_jellyfin(db: AsyncSession = Depends(get_db)):
    s = await cfg.get_all(db)
    url = s.get(cfg.JELLYFIN_URL)
    key = s.get(cfg.JELLYFIN_API_KEY)
    uid = s.get(cfg.JELLYFIN_USER_ID)
    if not url or not key or not uid:
        return {"ok": False, "message": "Jellyfin not configured"}
    client = JellyfinClient(url, key, uid)
    ok = await client.test_connection()
    return {"ok": ok, "message": "Connected" if ok else "Connection failed"}


@router.post("/test/pinepods")
async def test_pinepods(db: AsyncSession = Depends(get_db)):
    s = await cfg.get_all(db)
    url = s.get(cfg.PINEPODS_URL)
    key = s.get(cfg.PINEPODS_API_KEY)
    if not url or not key:
        return {"ok": False, "message": "Pinepods not configured"}
    client = PinepodsClient(url, key)
    ok = await client.test_connection()
    return {"ok": ok, "message": "Connected" if ok else "Connection failed"}


@router.get("/test/ipod")
async def test_ipod(db: AsyncSession = Depends(get_db)):
    s = await cfg.get_all(db)
    mount = s.get(cfg.IPOD_MOUNT) or IPodManager.find_ipod_mount(
        s.get(cfg.IPOD_LABEL, "IPOD") or "IPOD"
    )
    if not mount:
        return {"ok": False, "message": "iPod not found", "mount": None}
    ipod = IPodManager(mount)
    if not ipod.is_mounted():
        return {"ok": False, "message": "Mount point empty", "mount": mount}
    free = ipod.free_bytes()
    total = ipod.total_bytes()
    return {
        "ok": True,
        "message": "iPod found",
        "mount": mount,
        "free_gb": round(free / 1e9, 2),
        "total_gb": round(total / 1e9, 2),
    }


# ---------------------------------------------------------------------------
# Jellyfin libraries
# ---------------------------------------------------------------------------

@router.get("/jellyfin/libraries")
async def jellyfin_libraries(db: AsyncSession = Depends(get_db)):
    s = await cfg.get_all(db)
    url = s.get(cfg.JELLYFIN_URL)
    key = s.get(cfg.JELLYFIN_API_KEY)
    uid = s.get(cfg.JELLYFIN_USER_ID)
    if not url or not key or not uid:
        raise HTTPException(400, "Jellyfin not configured")
    client = JellyfinClient(url, key, uid)
    try:
        libs = await client.get_music_libraries()
        return libs
    except Exception as exc:
        raise HTTPException(500, str(exc))


# ---------------------------------------------------------------------------
# Pinepods podcasts (import)
# ---------------------------------------------------------------------------

@router.get("/pinepods/podcasts")
async def pinepods_podcasts(db: AsyncSession = Depends(get_db)):
    s = await cfg.get_all(db)
    url = s.get(cfg.PINEPODS_URL)
    key = s.get(cfg.PINEPODS_API_KEY)
    if not url or not key:
        raise HTTPException(400, "Pinepods not configured")
    client = PinepodsClient(url, key)
    try:
        pods = await client.get_podcasts()
        return [{"id": p.podcast_id, "title": p.title, "feed_url": p.feed_url} for p in pods]
    except Exception as exc:
        raise HTTPException(500, str(exc))


@router.get("/pinepods/debug")
async def pinepods_debug(db: AsyncSession = Depends(get_db)):
    """
    Returns the raw Pinepods API responses for debugging.
    Useful when podcasts don't show up — shows exactly what the server returns.
    """
    import httpx as _httpx
    s = await cfg.get_all(db)
    url = s.get(cfg.PINEPODS_URL)
    key = s.get(cfg.PINEPODS_API_KEY)
    if not url or not key:
        raise HTTPException(400, "Pinepods not configured")

    results: dict = {}
    headers = {"Api-Key": key}
    base = url.rstrip("/")

    async with _httpx.AsyncClient(headers=headers, timeout=15) as c:
        # 1. Check connectivity
        try:
            r = await c.get(f"{base}/api/pinepods_check")
            results["pinepods_check"] = {"status": r.status_code, "body": r.text[:500]}
        except Exception as exc:
            results["pinepods_check"] = {"error": str(exc)}

        # 2. Resolve user ID
        user_id = None
        try:
            r = await c.get(f"{base}/api/data/get_user")
            results["get_user"] = {"status": r.status_code, "body": r.json()}
            user_id = r.json().get("retrieved_id")
        except Exception as exc:
            results["get_user"] = {"error": str(exc)}

        # 3. Fetch podcasts
        if user_id:
            try:
                r = await c.get(f"{base}/api/data/return_pods/{user_id}")
                body = r.json()
                results["return_pods"] = {
                    "status": r.status_code,
                    "pod_count": len(body.get("pods", [])),
                    "first_pod_keys": list(body["pods"][0].keys()) if body.get("pods") else [],
                    "first_pod": body["pods"][0] if body.get("pods") else None,
                }
            except Exception as exc:
                results["return_pods"] = {"error": str(exc)}

    return results


# ---------------------------------------------------------------------------
# Podcast subscriptions
# ---------------------------------------------------------------------------

class PodcastAddPayload(BaseModel):
    title: str
    source: str = "rss"          # "rss" or "pinepods"
    feed_url: str | None = None
    pinepods_podcast_id: int | None = None
    max_episodes: int = 10


@router.get("/podcasts")
async def list_podcasts(db: AsyncSession = Depends(get_db)):
    subs = (await db.scalars(select(PodcastSubscription))).all()
    result = []
    for s in subs:
        ep_count = len((await db.scalars(
            select(PodcastEpisode).where(PodcastEpisode.subscription_id == s.id)
        )).all())
        on_device = len((await db.scalars(
            select(PodcastEpisode).where(
                PodcastEpisode.subscription_id == s.id,
                PodcastEpisode.on_device == True,  # noqa: E712
            )
        )).all())
        result.append({
            "id": s.id,
            "title": s.title,
            "source": s.source,
            "feed_url": s.feed_url,
            "pinepods_podcast_id": s.pinepods_podcast_id,
            "max_episodes": s.max_episodes,
            "enabled": s.enabled,
            "episode_count": ep_count,
            "on_device": on_device,
        })
    return result


@router.post("/podcasts")
async def add_podcast(payload: PodcastAddPayload, db: AsyncSession = Depends(get_db)):
    if payload.source == "rss":
        if not payload.feed_url:
            raise HTTPException(400, "feed_url required for RSS source")
        # Auto-fetch title if not provided or empty
        title = payload.title.strip()
        if not title:
            title = await rss_svc.fetch_feed_title(payload.feed_url)
    else:
        title = payload.title.strip() or "Unknown Podcast"

    sub = PodcastSubscription(
        title=title,
        source=payload.source,
        feed_url=payload.feed_url,
        pinepods_podcast_id=payload.pinepods_podcast_id,
        max_episodes=max(1, min(payload.max_episodes, 50)),
    )
    db.add(sub)
    await db.commit()
    await db.refresh(sub)
    return {"ok": True, "id": sub.id, "title": sub.title}


@router.delete("/podcasts/{sub_id}")
async def delete_podcast(sub_id: int, db: AsyncSession = Depends(get_db)):
    sub = await db.get(PodcastSubscription, sub_id)
    if not sub:
        raise HTTPException(404, "Not found")
    await db.delete(sub)
    await db.commit()
    return {"ok": True}


@router.patch("/podcasts/{sub_id}")
async def update_podcast(sub_id: int, payload: dict, db: AsyncSession = Depends(get_db)):
    sub = await db.get(PodcastSubscription, sub_id)
    if not sub:
        raise HTTPException(404, "Not found")
    if "max_episodes" in payload:
        sub.max_episodes = max(1, min(int(payload["max_episodes"]), 50))
    if "enabled" in payload:
        sub.enabled = bool(payload["enabled"])
    await db.commit()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Sync logs
# ---------------------------------------------------------------------------

@router.get("/logs")
async def get_logs(limit: int = 20, db: AsyncSession = Depends(get_db)):
    logs = (
        await db.scalars(
            select(SyncLog).order_by(SyncLog.started_at.desc()).limit(limit)
        )
    ).all()
    return [
        {
            "id": l.id,
            "started_at": l.started_at.isoformat() if l.started_at else None,
            "finished_at": l.finished_at.isoformat() if l.finished_at else None,
            "status": l.status,
            "music_added": l.music_added,
            "music_removed": l.music_removed,
            "podcasts_added": l.podcasts_added,
            "podcasts_removed": l.podcasts_removed,
            "message": l.message,
        }
        for l in logs
    ]


# ---------------------------------------------------------------------------
# Music stats
# ---------------------------------------------------------------------------

@router.get("/music/stats")
async def music_stats(db: AsyncSession = Depends(get_db)):
    tracks = (await db.scalars(select(MusicTrack))).all()
    on_device = sum(1 for t in tracks if t.on_device)
    return {"total": len(tracks), "on_device": on_device}
