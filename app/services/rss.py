"""
RSS feed parser for podcast subscriptions.

Uses feedparser to fetch and parse RSS/Atom podcast feeds.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime

import feedparser

log = logging.getLogger(__name__)


@dataclass
class RssEpisode:
    guid: str
    title: str
    url: str
    published_at: datetime | None
    duration_seconds: int | None


def _parse_duration(raw: str | None) -> int | None:
    """Convert HH:MM:SS or plain seconds string to integer seconds."""
    if not raw:
        return None
    raw = raw.strip()
    if ":" in raw:
        parts = raw.split(":")
        try:
            parts = [int(p) for p in parts]
            if len(parts) == 3:
                return parts[0] * 3600 + parts[1] * 60 + parts[2]
            if len(parts) == 2:
                return parts[0] * 60 + parts[1]
        except ValueError:
            pass
    try:
        return int(raw)
    except ValueError:
        return None


def _entry_guid(entry) -> str:
    """Best-effort stable GUID for a feed entry."""
    if hasattr(entry, "id") and entry.id:
        return entry.id
    # Fall back to a hash of the first enclosure URL
    for link in getattr(entry, "enclosures", []):
        if link.get("href"):
            return hashlib.sha1(link["href"].encode()).hexdigest()
    if hasattr(entry, "link") and entry.link:
        return hashlib.sha1(entry.link.encode()).hexdigest()
    return hashlib.sha1(entry.get("title", "").encode()).hexdigest()


def _entry_audio_url(entry) -> str | None:
    for enc in getattr(entry, "enclosures", []):
        mime = enc.get("type", "")
        if mime.startswith("audio/"):
            return enc.get("href")
    # fall back: any enclosure
    for enc in getattr(entry, "enclosures", []):
        href = enc.get("href")
        if href:
            return href
    return None


async def fetch_episodes(feed_url: str) -> list[RssEpisode]:
    """
    Parse a podcast RSS feed and return episodes, newest first.
    Uses feedparser (synchronous) run in a thread.
    """
    import asyncio

    loop = asyncio.get_event_loop()
    parsed = await loop.run_in_executor(None, feedparser.parse, feed_url)

    if parsed.bozo and not parsed.entries:
        log.warning("Failed to parse feed %s: %s", feed_url, parsed.bozo_exception)
        return []

    episodes: list[RssEpisode] = []
    for entry in parsed.entries:
        url = _entry_audio_url(entry)
        if not url:
            continue

        pub: datetime | None = None
        if hasattr(entry, "published_parsed") and entry.published_parsed:
            try:
                pub = datetime(*entry.published_parsed[:6])
            except Exception:
                pass

        duration_raw = None
        itunes = getattr(entry, "itunes_duration", None)
        if itunes:
            duration_raw = str(itunes)

        episodes.append(RssEpisode(
            guid=_entry_guid(entry),
            title=entry.get("title", "Unknown Episode"),
            url=url,
            published_at=pub,
            duration_seconds=_parse_duration(duration_raw),
        ))

    # Sort newest first (None dates go last)
    episodes.sort(
        key=lambda e: e.published_at or datetime.min,
        reverse=True,
    )
    return episodes


async def fetch_feed_title(feed_url: str) -> str:
    import asyncio
    loop = asyncio.get_event_loop()
    parsed = await loop.run_in_executor(None, feedparser.parse, feed_url)
    return parsed.feed.get("title") or feed_url
