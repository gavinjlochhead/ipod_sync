"""
Pinepods API client.

Pinepods exposes a REST API for managing podcast subscriptions and
playback state.  We use it to:
  - List subscribed podcasts
  - List episodes for a podcast
  - Check which episodes have been played
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

    async def get_podcasts(self, user_id: int = 1) -> list[PinepodsPodcast]:
        async with self._client() as c:
            r = await c.get(
                f"{self.base_url}/api/data/return_pods",
                params={"user_id": user_id},
            )
            r.raise_for_status()
            data = r.json()
            podcasts = []
            for pod in data.get("pods", []):
                podcasts.append(PinepodsPodcast(
                    podcast_id=pod["PodcastID"],
                    title=pod["PodcastName"],
                    feed_url=pod.get("FeedURL", ""),
                ))
            return podcasts

    async def get_episodes(
        self, podcast_id: int, user_id: int = 1
    ) -> list[PinepodsEpisode]:
        async with self._client() as c:
            r = await c.get(
                f"{self.base_url}/api/data/return_episodes",
                params={"podcast_id": podcast_id, "user_id": user_id},
            )
            r.raise_for_status()
            data = r.json()
            episodes = []
            for ep in data.get("episodes", []):
                pub = None
                if ep.get("PubDate"):
                    try:
                        pub = datetime.fromisoformat(ep["PubDate"])
                    except ValueError:
                        pass

                episodes.append(PinepodsEpisode(
                    episode_id=ep["EpisodeID"],
                    podcast_id=podcast_id,
                    title=ep.get("EpisodeTitle", "Unknown"),
                    url=ep.get("EpisodeURL", ""),
                    guid=ep.get("EpisodeURL", str(ep["EpisodeID"])),
                    published_at=pub,
                    duration=ep.get("EpisodeDuration"),
                    played=bool(ep.get("Completed", False)),
                    listen_duration=ep.get("ListenDuration"),
                ))
            return episodes

    async def get_played_episodes(self, user_id: int = 1) -> set[str]:
        """Return a set of GUIDs (episode URLs) that have been fully played."""
        async with self._client() as c:
            r = await c.get(
                f"{self.base_url}/api/data/return_eps_historylength",
                params={"user_id": user_id, "episode_history": 1000},
            )
            if r.status_code != 200:
                return set()
            data = r.json()
            return {
                ep.get("EpisodeURL", "")
                for ep in data.get("episodes", [])
                if ep.get("Completed")
            }
