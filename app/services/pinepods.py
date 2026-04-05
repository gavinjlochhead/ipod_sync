"""
Pinepods API client.

Pinepods exposes a Rust-based REST API.  Confirmed endpoint shapes
from the PinePods source (rust-api/src/handlers/ and models.rs):

  GET /api/data/get_user
    → {"status": "...", "retrieved_id": <int>}
    Resolves the numeric user ID from the Api-Key header.

  GET /api/data/return_pods/{user_id}
    → {"pods": [{"podcastid": int, "podcastname": str, "feedurl": str, ...}]}
    All field names are lowercase.

  GET /api/data/podcast_episodes?user_id=X&podcast_id=Y
    → {"episodes": [{"Episodetitle": str, "Episodeurl": str,
                      "Episodepubdate": str, "Episodeduration": int,
                      "Episodeid": int, "Completed": bool,
                      "Listenduration": int|null, ...}]}
    Mixed case: episode fields are Title-cased, a few are lowercase.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

import httpx

log = logging.getLogger(__name__)


@dataclass
class PinepodsEpisode:
    episode_id: int
    podcast_id: int
    title: str
    url: str
    guid: str
    published_at: datetime | None
    duration: int | None     # seconds
    played: bool
    listen_duration: int | None  # seconds listened


@dataclass
class PinepodsPodcast:
    podcast_id: int
    title: str
    feed_url: str
    episodes: list[PinepodsEpisode] = field(default_factory=list)


class PinepodsClient:
    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            headers={"Api-Key": self.api_key},
            timeout=30,
        )

    async def test_connection(self) -> bool:
        try:
            async with self._client() as c:
                r = await c.get(f"{self.base_url}/api/pinepods_check")
                return r.status_code == 200
        except Exception as exc:
            log.warning("Pinepods connection test failed: %s", exc)
            return False

    async def get_user_id(self) -> int:
        """
        Resolve the numeric user ID from the API key.
        Calls GET /api/data/get_user → {"status": "...", "retrieved_id": int}
        """
        async with self._client() as c:
            r = await c.get(f"{self.base_url}/api/data/get_user")
            r.raise_for_status()
            data = r.json()
            user_id = data.get("retrieved_id")
            if user_id is None:
                raise ValueError(
                    f"Pinepods get_user returned unexpected payload: {data}"
                )
            return int(user_id)

    async def get_podcasts(self) -> list[PinepodsPodcast]:
        """
        List all podcasts subscribed by the current user.
        GET /api/data/return_pods/{user_id}
        Response: {"pods": [{"podcastid": int, "podcastname": str,
                              "feedurl": str, ...}]}
        """
        user_id = await self.get_user_id()
        async with self._client() as c:
            r = await c.get(
                f"{self.base_url}/api/data/return_pods/{user_id}"
            )
            r.raise_for_status()
            data = r.json()
            log.debug("return_pods raw: %s", data)

            podcasts = []
            for pod in data.get("pods", []):
                podcasts.append(PinepodsPodcast(
                    podcast_id=pod["podcastid"],
                    title=pod["podcastname"],
                    feed_url=pod.get("feedurl", ""),
                ))
            return podcasts

    async def get_episodes(
        self, podcast_id: int, user_id: int | None = None
    ) -> list[PinepodsEpisode]:
        """
        Get episodes for a specific podcast.
        GET /api/data/podcast_episodes?user_id=X&podcast_id=Y

        Response field names are Title-cased for episode fields:
          Episodetitle, Episodeurl, Episodepubdate, Episodeduration,
          Episodeid, Completed, Listenduration
        """
        if user_id is None:
            user_id = await self.get_user_id()

        async with self._client() as c:
            r = await c.get(
                f"{self.base_url}/api/data/podcast_episodes",
                params={"user_id": user_id, "podcast_id": podcast_id},
            )
            r.raise_for_status()
            data = r.json()
            log.debug("podcast_episodes raw (podcast %s): %s", podcast_id, data)

            episodes = []
            for ep in data.get("episodes", []):
                pub: datetime | None = None
                pub_str = ep.get("Episodepubdate") or ep.get("episodepubdate")
                if pub_str:
                    try:
                        pub = datetime.fromisoformat(
                            pub_str.replace("Z", "+00:00")
                        )
                    except ValueError:
                        pass

                url = ep.get("Episodeurl") or ep.get("episodeurl") or ""
                ep_id = ep.get("Episodeid") or ep.get("episodeid") or 0

                episodes.append(PinepodsEpisode(
                    episode_id=int(ep_id),
                    podcast_id=podcast_id,
                    title=ep.get("Episodetitle") or ep.get("episodetitle") or "Unknown",
                    url=url,
                    guid=url or str(ep_id),
                    published_at=pub,
                    duration=ep.get("Episodeduration") or ep.get("episodeduration"),
                    played=bool(ep.get("Completed") or ep.get("completed", False)),
                    listen_duration=ep.get("Listenduration") or ep.get("listenduration"),
                ))
            return episodes

    async def get_played_episodes(self, user_id: int | None = None) -> set[str]:
        """
        Return a set of episode URLs that have been fully played.

        We derive this from the podcast_episodes endpoint rather than a
        dedicated history endpoint, iterating over all subscribed podcasts.
        For large libraries this is acceptable since we only call it once
        per sync and cache the result in a set.
        """
        if user_id is None:
            try:
                user_id = await self.get_user_id()
            except Exception as exc:
                log.warning("Could not resolve user ID for played check: %s", exc)
                return set()

        played: set[str] = set()
        try:
            podcasts = await self.get_podcasts()
            for pod in podcasts:
                try:
                    episodes = await self.get_episodes(pod.podcast_id, user_id)
                    for ep in episodes:
                        if ep.played and ep.url:
                            played.add(ep.url)
                except Exception as exc:
                    log.debug("Skipping played check for podcast %s: %s", pod.title, exc)
        except Exception as exc:
            log.warning("get_played_episodes failed: %s", exc)

        return played
