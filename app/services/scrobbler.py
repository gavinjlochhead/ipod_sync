"""
Rockbox .scrobbler.log parser and Jellyfin play-history sync.

Rockbox writes /Podcasts-free listen history to:
  <ipod_root>/.scrobbler.log

Format (tab-separated, one entry per line after the 3-line header):
  artist  album  title  tracknum  duration_secs  rating  timestamp_unix  mbid

  rating = "L" (listened/completed) or "S" (skipped)

After we read and sync the entries we rename the log to
.scrobbler.log.bak so Rockbox starts a fresh one on next boot.
"""
from __future__ import annotations

import datetime
import logging
import os
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

SCROBBLER_LOG = ".scrobbler.log"
SCROBBLER_BAK = ".scrobbler.log.bak"


@dataclass
class ScrobbleEntry:
    artist: str
    album: str
    title: str
    track_number: int | None
    duration_secs: int | None
    # "L" = listened, "S" = skipped
    rating: str
    timestamp: datetime.datetime | None
    mbid: str | None

    @property
    def was_played(self) -> bool:
        return self.rating.upper() == "L"


def parse_scrobbler_log(ipod_mount: str) -> list[ScrobbleEntry]:
    """
    Parse the Rockbox scrobbler log and return all entries.
    Returns an empty list if the log does not exist.
    """
    log_path = Path(ipod_mount) / SCROBBLER_LOG
    if not log_path.exists():
        log.debug("No .scrobbler.log found at %s", ipod_mount)
        return []

    entries: list[ScrobbleEntry] = []

    with open(log_path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) < 6:
                continue

            # Pad to 8 fields
            parts += [""] * (8 - len(parts))

            try:
                track_num = int(parts[3]) if parts[3] else None
            except ValueError:
                track_num = None

            try:
                duration = int(parts[4]) if parts[4] else None
            except ValueError:
                duration = None

            try:
                ts_unix = int(parts[6]) if parts[6] else None
                ts = datetime.datetime.utcfromtimestamp(ts_unix) if ts_unix else None
            except (ValueError, OSError):
                ts = None

            entries.append(ScrobbleEntry(
                artist=parts[0],
                album=parts[1],
                title=parts[2],
                track_number=track_num,
                duration_secs=duration,
                rating=parts[5] or "S",
                timestamp=ts,
                mbid=parts[7] or None,
            ))

    log.info("Parsed %d scrobble entries from .scrobbler.log", len(entries))
    return entries


def archive_scrobbler_log(ipod_mount: str) -> None:
    """
    Rename .scrobbler.log → .scrobbler.log.bak so Rockbox starts fresh.
    Any previous .bak is overwritten.
    """
    log_path = Path(ipod_mount) / SCROBBLER_LOG
    bak_path = Path(ipod_mount) / SCROBBLER_BAK
    if log_path.exists():
        log_path.rename(bak_path)
        log.info("Archived .scrobbler.log → .scrobbler.log.bak")


async def sync_scrobbles_to_jellyfin(
    entries: list[ScrobbleEntry],
    session_factory,
) -> tuple[int, int]:
    """
    Match scrobble entries against our MusicTrack DB records and mark
    them as played in Jellyfin.

    Returns (matched, failed).
    """
    from sqlalchemy import select
    from app.models import MusicTrack
    from app import config as cfg
    from app.services.jellyfin import JellyfinClient

    played_entries = [e for e in entries if e.was_played]
    if not played_entries:
        return 0, 0

    async with session_factory() as db:
        settings = await cfg.get_all(db)
        tracks = (await db.scalars(select(MusicTrack).where(MusicTrack.on_device == True))).all()  # noqa: E712

    url = settings.get(cfg.JELLYFIN_URL)
    api_key = settings.get(cfg.JELLYFIN_API_KEY)
    user_id = settings.get(cfg.JELLYFIN_USER_ID)

    if not url or not api_key or not user_id:
        log.info("Jellyfin not configured — skipping scrobble sync")
        return 0, 0

    # Build lookup: (title_lower, artist_lower) -> jellyfin_id
    track_map: dict[tuple[str, str], str] = {}
    for t in tracks:
        key = (t.title.lower().strip(), (t.artist or "").lower().strip())
        track_map[key] = t.jellyfin_id

    client = JellyfinClient(url, api_key, user_id)
    matched = 0
    failed = 0

    for entry in played_entries:
        key = (entry.title.lower().strip(), entry.artist.lower().strip())
        jellyfin_id = track_map.get(key)

        if not jellyfin_id:
            # Try title-only match as fallback
            for (title, _), jid in track_map.items():
                if title == entry.title.lower().strip():
                    jellyfin_id = jid
                    break

        if not jellyfin_id:
            log.debug("No match for scrobble: %s — %s", entry.artist, entry.title)
            failed += 1
            continue

        try:
            await client.mark_played(jellyfin_id, entry.timestamp)
            matched += 1
            log.info("Marked played in Jellyfin: %s — %s", entry.artist, entry.title)
        except Exception as exc:
            log.warning("Failed to mark %s played: %s", entry.title, exc)
            failed += 1

    return matched, failed
