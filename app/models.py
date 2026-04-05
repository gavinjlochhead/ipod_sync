from __future__ import annotations

import datetime
from sqlalchemy import (
    Integer, String, Boolean, DateTime, Text, ForeignKey, UniqueConstraint
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.database import Base


class Setting(Base):
    """Key/value store for all app configuration."""
    __tablename__ = "settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    value: Mapped[str | None] = mapped_column(Text, nullable=True)


class PodcastSubscription(Base):
    __tablename__ = "podcast_subscriptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    # source: "rss" or "pinepods"
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="rss")
    # For RSS: the feed URL. For Pinepods: the podcast ID in Pinepods.
    feed_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    pinepods_podcast_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_episodes: Mapped[int] = mapped_column(Integer, default=10)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=datetime.datetime.utcnow
    )

    episodes: Mapped[list[PodcastEpisode]] = relationship(
        "PodcastEpisode", back_populates="subscription", cascade="all, delete-orphan"
    )


class PodcastEpisode(Base):
    __tablename__ = "podcast_episodes"
    __table_args__ = (UniqueConstraint("subscription_id", "guid"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    subscription_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("podcast_subscriptions.id"), nullable=False
    )
    guid: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    published_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # on_device: has the file been copied to the iPod
    on_device: Mapped[bool] = mapped_column(Boolean, default=False)
    # device_path: relative path on the iPod filesystem
    device_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    # played: marked as played (from Pinepods or tracked locally)
    played: Mapped[bool] = mapped_column(Boolean, default=False)
    # downloaded: local cache path
    local_path: Mapped[str | None] = mapped_column(Text, nullable=True)

    subscription: Mapped[PodcastSubscription] = relationship(
        "PodcastSubscription", back_populates="episodes"
    )


class MusicTrack(Base):
    """Tracks which Jellyfin items have been synced to the device."""
    __tablename__ = "music_tracks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    jellyfin_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    artist: Mapped[str | None] = mapped_column(Text, nullable=True)
    album: Mapped[str | None] = mapped_column(Text, nullable=True)
    track_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    device_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    on_device: Mapped[bool] = mapped_column(Boolean, default=False)
    file_size: Mapped[int | None] = mapped_column(Integer, nullable=True)


class SyncLog(Base):
    __tablename__ = "sync_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    started_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=datetime.datetime.utcnow
    )
    finished_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    # "success", "error", "running", "cancelled"
    status: Mapped[str] = mapped_column(String(32), default="running")
    music_added: Mapped[int] = mapped_column(Integer, default=0)
    music_removed: Mapped[int] = mapped_column(Integer, default=0)
    podcasts_added: Mapped[int] = mapped_column(Integer, default=0)
    podcasts_removed: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
