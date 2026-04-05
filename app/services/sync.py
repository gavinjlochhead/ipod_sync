"""
Sync orchestrator.

Coordinates music (Jellyfin) and podcast syncing to the iPod.
Runs as a background task; uses an asyncio.Lock to prevent concurrent runs.
Progress and results are written to SyncLog in the database.
"""
from __future__ import annotations

import asyncio
import datetime
import logging
import os
import re
import tempfile
from pathlib import Path

import httpx
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import config as cfg
from app.models import MusicTrack, PodcastEpisode, PodcastSubscription, SyncLog
from app.services.ipod import IPodManager
from app.services.jellyfin import JellyfinClient, JellyfinTrack
from app.services.pinepods import PinepodsClient
from app.services import rss as rss_svc
from app.services import scrobbler as scrobbler_svc
from app.services import mqtt_pub

log = logging.getLogger(__name__)

_sync_lock = asyncio.Lock()
_current_log_id: int | None = None
_sync_status: dict = {"running": False, "message": "Idle"}


def get_status() -> dict:
    return dict(_sync_status)


async def run_sync(session_factory: async_sessionmaker) -> None:
    global _current_log_id, _sync_status

    if _sync_lock.locked():
        log.info("Sync already running — skipping")
        return

    async with _sync_lock:
        _sync_status["running"] = True
        _sync_status["message"] = "Starting sync…"

        async with session_factory() as db:
            sync_log = SyncLog(started_at=datetime.datetime.utcnow(), status="running")
            db.add(sync_log)
            await db.commit()
            await db.refresh(sync_log)
            _current_log_id = sync_log.id

        try:
            async with session_factory() as db:
                settings = await cfg.get_all(db)

            mount = settings.get(cfg.IPOD_MOUNT) or IPodManager.find_ipod_mount(
                settings.get(cfg.IPOD_LABEL, "IPOD") or "IPOD"
            )

            if not mount or not Path(mount).is_dir():
                raise RuntimeError(
                    f"iPod not found at mount point '{mount}'. "
                    "Check settings or plug in your iPod."
                )

            ipod = IPodManager(mount)
            if not ipod.is_mounted():
                raise RuntimeError("iPod mount directory is empty — device not mounted?")

            # Step 1: Read Rockbox scrobbler log and sync play history back to Jellyfin
            scrobble_matched = 0
            if (settings.get(cfg.SCROBBLE_TO_JELLYFIN, "true") or "true").lower() == "true":
                _sync_status["message"] = "Reading play history from iPod…"
                mqtt_pub.publish_status(_sync_status)
                scrobble_entries = scrobbler_svc.parse_scrobbler_log(mount)
                if scrobble_entries:
                    scrobble_matched, _ = await scrobbler_svc.sync_scrobbles_to_jellyfin(
                        scrobble_entries, session_factory
                    )
                    scrobbler_svc.archive_scrobbler_log(mount)

            _sync_status["message"] = "Syncing music…"
            mqtt_pub.publish_status(_sync_status)
            music_added, music_removed = await _sync_music(session_factory, settings, ipod)

            _sync_status["message"] = "Syncing podcasts…"
            mqtt_pub.publish_status(_sync_status)
            pod_added, pod_removed = await _sync_podcasts(session_factory, settings, ipod)

            async with session_factory() as db:
                await db.execute(
                    update(SyncLog)
                    .where(SyncLog.id == _current_log_id)
                    .values(
                        status="success",
                        finished_at=datetime.datetime.utcnow(),
                        music_added=music_added,
                        music_removed=music_removed,
                        podcasts_added=pod_added,
                        podcasts_removed=pod_removed,
                        message=f"Music +{music_added}/-{music_removed}, "
                                f"Podcasts +{pod_added}/-{pod_removed}",
                    )
                )
                await db.commit()

            msg = (
                f"Done — Music +{music_added}/-{music_removed}, "
                f"Podcasts +{pod_added}/-{pod_removed}"
            )
            if scrobble_matched:
                msg += f", {scrobble_matched} plays synced to Jellyfin"
            _sync_status["message"] = msg
            mqtt_pub.publish_status(_sync_status)

        except Exception as exc:
            log.exception("Sync failed: %s", exc)
            _sync_status["message"] = f"Error: {exc}"
            mqtt_pub.publish_status(_sync_status)
            async with session_factory() as db:
                await db.execute(
                    update(SyncLog)
                    .where(SyncLog.id == _current_log_id)
                    .values(
                        status="error",
                        finished_at=datetime.datetime.utcnow(),
                        message=str(exc),
                    )
                )
                await db.commit()
        finally:
            _sync_status["running"] = False


# ---------------------------------------------------------------------------
# Music sync
# ---------------------------------------------------------------------------

async def _sync_music(
    session_factory: async_sessionmaker,
    settings: dict,
    ipod: IPodManager,
) -> tuple[int, int]:
    url = settings.get(cfg.JELLYFIN_URL)
    api_key = settings.get(cfg.JELLYFIN_API_KEY)
    user_id = settings.get(cfg.JELLYFIN_USER_ID)
    library_id = settings.get(cfg.JELLYFIN_LIBRARY_ID)

    if not url or not api_key or not user_id:
        log.info("Jellyfin not configured — skipping music sync")
        return 0, 0

    client = JellyfinClient(url, api_key, user_id)
    added = 0
    removed = 0

    async with session_factory() as db:
        existing: dict[str, MusicTrack] = {
            t.jellyfin_id: t
            for t in (await db.scalars(select(MusicTrack))).all()
        }

    seen_ids: set[str] = set()

    async for track in client.iter_all_tracks(library_id):
        seen_ids.add(track.item_id)
        db_track = existing.get(track.item_id)

        if db_track and db_track.on_device:
            # Already on device — verify file still exists
            if db_track.device_path and (ipod.mount / db_track.device_path).exists():
                continue

        # Need to copy to device
        _sync_status["message"] = f"Music: copying {track.title!r}…"
        filename = ipod.track_filename(track)

        with tempfile.TemporaryDirectory() as tmp:
            tmp_file = os.path.join(tmp, filename)
            try:
                await client.download_track(track.item_id, tmp_file)
            except Exception as exc:
                log.warning("Failed to download %s: %s", track.title, exc)
                continue

            rel_path = await ipod.copy_music_track(
                tmp_file, track.artist, track.album, filename
            )

        async with session_factory() as db:
            if db_track is None:
                db_track = MusicTrack(
                    jellyfin_id=track.item_id,
                    title=track.title,
                    artist=track.artist,
                    album=track.album,
                    track_number=track.track_number,
                )
                db.add(db_track)
            db_track.device_path = rel_path
            db_track.on_device = True
            db_track.file_size = track.file_size
            await db.commit()

        added += 1
        log.info("Music: copied %s", track.title)

    # Remove tracks no longer in Jellyfin (if option enabled)
    remove_deleted = (settings.get(cfg.MUSIC_REMOVE_DELETED) or "false").lower() == "true"
    if remove_deleted:
        stale = [t for t in existing.values() if t.jellyfin_id not in seen_ids and t.on_device]
        for track in stale:
            if track.device_path:
                ipod.remove_music_track(track.device_path)
            async with session_factory() as db:
                t = await db.get(MusicTrack, track.id)
                if t:
                    t.on_device = False
                    t.device_path = None
                    await db.commit()
            removed += 1
            log.info("Music: removed stale track %s", track.title)

    return added, removed


# ---------------------------------------------------------------------------
# Podcast sync
# ---------------------------------------------------------------------------

async def _sync_podcasts(
    session_factory: async_sessionmaker,
    settings: dict,
    ipod: IPodManager,
) -> tuple[int, int]:
    added = 0
    removed = 0

    cache_dir = settings.get(cfg.CACHE_DIR) or "/var/cache/ipod-sync/podcasts"
    os.makedirs(cache_dir, exist_ok=True)

    # Build Pinepods played set if configured
    played_guids: set[str] = set()
    pinepods_url = settings.get(cfg.PINEPODS_URL)
    pinepods_key = settings.get(cfg.PINEPODS_API_KEY)
    pinepods_client: PinepodsClient | None = None
    if pinepods_url and pinepods_key:
        pinepods_client = PinepodsClient(pinepods_url, pinepods_key)
        played_guids = await pinepods_client.get_played_episodes()

    async with session_factory() as db:
        subscriptions = (
            await db.scalars(
                select(PodcastSubscription).where(PodcastSubscription.enabled == True)  # noqa: E712
            )
        ).all()

    for sub in subscriptions:
        _sync_status["message"] = f"Podcasts: refreshing {sub.title!r}…"
        try:
            a, r = await _sync_one_podcast(
                session_factory, settings, ipod, sub,
                pinepods_client, played_guids, cache_dir,
            )
            added += a
            removed += r
        except Exception as exc:
            log.warning("Failed to sync podcast %s: %s", sub.title, exc)

    return added, removed


async def _sync_one_podcast(
    session_factory: async_sessionmaker,
    settings: dict,
    ipod: IPodManager,
    sub: PodcastSubscription,
    pinepods_client: PinepodsClient | None,
    played_guids: set[str],
    cache_dir: str,
) -> tuple[int, int]:
    added = 0
    removed = 0

    # 1. Fetch episode list from source
    if sub.source == "pinepods" and pinepods_client and sub.pinepods_podcast_id:
        episodes_raw = await pinepods_client.get_episodes(sub.pinepods_podcast_id)
        remote_episodes = [
            {
                "guid": ep.guid,
                "title": ep.title,
                "url": ep.url,
                "published_at": ep.published_at,
                "duration": ep.duration,
                "played": ep.played or ep.guid in played_guids,
            }
            for ep in episodes_raw
        ]
    else:
        # RSS feed
        feed_url = sub.feed_url
        if not feed_url:
            log.warning("Podcast %s has no feed URL", sub.title)
            return 0, 0
        rss_episodes = await rss_svc.fetch_episodes(feed_url)
        remote_episodes = [
            {
                "guid": ep.guid,
                "title": ep.title,
                "url": ep.url,
                "published_at": ep.published_at,
                "duration": ep.duration_seconds,
                "played": ep.guid in played_guids,
            }
            for ep in rss_episodes
        ]

    # 2. Upsert episodes into DB
    async with session_factory() as db:
        db_episodes: dict[str, PodcastEpisode] = {
            ep.guid: ep
            for ep in (
                await db.scalars(
                    select(PodcastEpisode).where(
                        PodcastEpisode.subscription_id == sub.id
                    )
                )
            ).all()
        }

        for ep_data in remote_episodes:
            ep = db_episodes.get(ep_data["guid"])
            if ep is None:
                ep = PodcastEpisode(
                    subscription_id=sub.id,
                    guid=ep_data["guid"],
                    title=ep_data["title"],
                    url=ep_data["url"],
                    published_at=ep_data["published_at"],
                    duration_seconds=ep_data["duration"],
                    played=ep_data["played"],
                )
                db.add(ep)
                db_episodes[ep_data["guid"]] = ep
            else:
                ep.played = ep_data["played"]
                ep.url = ep_data["url"]
        await db.commit()

    # 3. Remove played episodes from device
    async with session_factory() as db:
        played_on_device = (
            await db.scalars(
                select(PodcastEpisode).where(
                    PodcastEpisode.subscription_id == sub.id,
                    PodcastEpisode.on_device == True,  # noqa: E712
                    PodcastEpisode.played == True,  # noqa: E712
                )
            )
        ).all()

        for ep in played_on_device:
            if ep.device_path:
                ipod.remove_podcast_episode(ep.device_path)
                # Clean up local cache
                if ep.local_path and os.path.exists(ep.local_path):
                    os.unlink(ep.local_path)
            ep.on_device = False
            ep.device_path = None
            removed += 1
            log.info("Podcast: removed played episode %s", ep.title)

        await db.commit()

    # 4. Determine which episodes to keep (newest unplayed, up to max_episodes)
    async with session_factory() as db:
        all_unplayed = (
            await db.scalars(
                select(PodcastEpisode)
                .where(
                    PodcastEpisode.subscription_id == sub.id,
                    PodcastEpisode.played == False,  # noqa: E712
                )
                .order_by(PodcastEpisode.published_at.desc().nullslast())
            )
        ).all()

    keep = all_unplayed[: sub.max_episodes]
    keep_guids = {ep.guid for ep in keep}

    # Remove episodes on device that exceed the limit
    async with session_factory() as db:
        overflow = (
            await db.scalars(
                select(PodcastEpisode).where(
                    PodcastEpisode.subscription_id == sub.id,
                    PodcastEpisode.on_device == True,  # noqa: E712
                    PodcastEpisode.played == False,  # noqa: E712
                )
            )
        ).all()
        for ep in overflow:
            if ep.guid not in keep_guids:
                if ep.device_path:
                    ipod.remove_podcast_episode(ep.device_path)
                    if ep.local_path and os.path.exists(ep.local_path):
                        os.unlink(ep.local_path)
                ep.on_device = False
                ep.device_path = None
                removed += 1
        await db.commit()

    # 5. Download and copy episodes not yet on device
    async with session_factory() as db:
        to_copy = (
            await db.scalars(
                select(PodcastEpisode).where(
                    PodcastEpisode.subscription_id == sub.id,
                    PodcastEpisode.on_device == False,  # noqa: E712
                    PodcastEpisode.played == False,  # noqa: E712
                    PodcastEpisode.guid.in_(keep_guids),
                )
            )
        ).all()

    for ep in to_copy:
        _sync_status["message"] = f"Podcasts: downloading {ep.title!r}…"
        ext = _ext_from_url(ep.url)
        safe_title = re.sub(r"[^\w\s-]", "", ep.title)[:60].strip()
        filename = f"{safe_title}{ext}"
        local_path = os.path.join(cache_dir, f"{sub.id}_{ep.id}{ext}")

        try:
            await _download_file(ep.url, local_path)
        except Exception as exc:
            log.warning("Failed to download episode %s: %s", ep.title, exc)
            continue

        rel_path = await ipod.copy_podcast_episode(local_path, sub.title, filename)

        async with session_factory() as db:
            ep_db = await db.get(PodcastEpisode, ep.id)
            if ep_db:
                ep_db.on_device = True
                ep_db.device_path = rel_path
                ep_db.local_path = local_path
                await db.commit()

        added += 1
        log.info("Podcast: copied %s", ep.title)

    return added, removed


def _ext_from_url(url: str) -> str:
    path = url.split("?")[0]
    if "." in path.split("/")[-1]:
        return "." + path.rsplit(".", 1)[-1].lower()
    return ".mp3"


async def _download_file(url: str, dest: str) -> None:
    import aiofiles
    async with httpx.AsyncClient(timeout=120, follow_redirects=True) as c:
        async with c.stream("GET", url) as r:
            r.raise_for_status()
            async with aiofiles.open(dest, "wb") as f:
                async for chunk in r.aiter_bytes(65536):
                    await f.write(chunk)
