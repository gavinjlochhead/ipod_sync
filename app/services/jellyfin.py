"""
Jellyfin API client.

Fetches the full music library and provides a streaming URL for each track.
Authentication uses an API key (preferred) or username/password.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import AsyncIterator

import httpx

log = logging.getLogger(__name__)


@dataclass
class JellyfinTrack:
    item_id: str
    title: str
    artist: str
    album: str
    album_artist: str
    track_number: int | None
    disc_number: int | None
    duration_ticks: int | None
    container: str          # mp3, flac, ogg, …
    file_size: int | None

    @property
    def duration_seconds(self) -> int | None:
        if self.duration_ticks is None:
            return None
        return self.duration_ticks // 10_000_000


class JellyfinClient:
    def __init__(self, base_url: str, api_key: str, user_id: str):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.user_id = user_id
        self._headers = {
            "Authorization": (
                f'MediaBrowser Client="ipod-sync", Device="RaspberryPi", '
                f'DeviceId="ipod-sync-rpi", Version="1.0", Token="{api_key}"'
            )
        }

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(headers=self._headers, timeout=30)

    async def test_connection(self) -> bool:
        try:
            async with self._client() as c:
                r = await c.get(f"{self.base_url}/System/Info")
                return r.status_code == 200
        except Exception as exc:
            log.warning("Jellyfin connection test failed: %s", exc)
            return False

    async def get_user_id(self) -> str | None:
        """Return the first admin user ID if not explicitly set."""
        async with self._client() as c:
            r = await c.get(f"{self.base_url}/Users")
            r.raise_for_status()
            users = r.json()
            if users:
                return users[0]["Id"]
        return None

    async def get_music_libraries(self) -> list[dict]:
        async with self._client() as c:
            r = await c.get(
                f"{self.base_url}/Users/{self.user_id}/Views",
            )
            r.raise_for_status()
            return [
                item for item in r.json().get("Items", [])
                if item.get("CollectionType") == "music"
            ]

    async def iter_all_tracks(
        self, library_id: str | None = None
    ) -> AsyncIterator[JellyfinTrack]:
        """Yield every music track from the library, paginated."""
        start = 0
        limit = 500

        async with self._client() as c:
            while True:
                params: dict = {
                    "IncludeItemTypes": "Audio",
                    "Recursive": "true",
                    "StartIndex": start,
                    "Limit": limit,
                    "Fields": "MediaSources,Path,ParentId",
                    "SortBy": "AlbumArtist,Album,SortName",
                    "SortOrder": "Ascending",
                }
                if library_id:
                    params["ParentId"] = library_id

                r = await c.get(
                    f"{self.base_url}/Users/{self.user_id}/Items",
                    params=params,
                )
                r.raise_for_status()
                data = r.json()
                items = data.get("Items", [])
                if not items:
                    break

                for item in items:
                    media_sources = item.get("MediaSources") or []
                    container = "mp3"
                    file_size = None
                    if media_sources:
                        container = media_sources[0].get("Container", "mp3")
                        file_size = media_sources[0].get("Size")

                    yield JellyfinTrack(
                        item_id=item["Id"],
                        title=item.get("Name", "Unknown"),
                        artist=item.get("AlbumArtist") or (
                            ", ".join(item.get("Artists", [])) or "Unknown Artist"
                        ),
                        album=item.get("Album", "Unknown Album"),
                        album_artist=item.get("AlbumArtist", "Unknown Artist"),
                        track_number=item.get("IndexNumber"),
                        disc_number=item.get("ParentIndexNumber"),
                        duration_ticks=item.get("RunTimeTicks"),
                        container=container,
                        file_size=file_size,
                    )

                start += len(items)
                if start >= data.get("TotalRecordCount", 0):
                    break

    def stream_url(self, item_id: str, container: str = "mp3") -> str:
        """
        Direct stream URL.  We request the original container so we copy
        without re-encoding (Rockbox supports mp3/flac/ogg/aac/m4a).
        """
        return (
            f"{self.base_url}/Audio/{item_id}/stream"
            f"?static=true&api_key={self.api_key}"
        )

    async def download_track(self, item_id: str, dest_path: str) -> None:
        """Stream a track directly to disk."""
        import aiofiles
        url = self.stream_url(item_id)
        async with self._client() as c:
            async with c.stream("GET", url, follow_redirects=True) as r:
                r.raise_for_status()
                async with aiofiles.open(dest_path, "wb") as f:
                    async for chunk in r.aiter_bytes(65536):
                        await f.write(chunk)

    async def mark_played(
        self,
        item_id: str,
        played_at: "datetime.datetime | None" = None,
    ) -> None:
        """
        Mark a media item as played in Jellyfin.
        Uses POST /Users/{userId}/PlayedItems/{itemId}.
        """
        import datetime as _dt
        params = {}
        if played_at:
            # Jellyfin expects ISO-8601 with Z suffix
            params["datePlayed"] = played_at.strftime("%Y-%m-%dT%H:%M:%S.0000000Z")
        async with self._client() as c:
            r = await c.post(
                f"{self.base_url}/Users/{self.user_id}/PlayedItems/{item_id}",
                params=params,
            )
            r.raise_for_status()
