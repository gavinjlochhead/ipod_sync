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
    })
    return {"ok": True}


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
