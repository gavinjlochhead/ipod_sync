"""
Thin helpers for reading/writing settings stored in the DB.
Keys are plain strings; all values are stored as text.
"""
from __future__ import annotations
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.models import Setting

# Keys used throughout the app
JELLYFIN_URL = "jellyfin_url"
JELLYFIN_API_KEY = "jellyfin_api_key"
JELLYFIN_USER_ID = "jellyfin_user_id"
JELLYFIN_LIBRARY_ID = "jellyfin_library_id"

PINEPODS_URL = "pinepods_url"
PINEPODS_API_KEY = "pinepods_api_key"

IPOD_MOUNT = "ipod_mount"          # e.g. /media/ipod
IPOD_LABEL = "ipod_label"          # filesystem label, e.g. IPOD
CACHE_DIR = "cache_dir"            # local dir for downloaded podcasts

MUSIC_REMOVE_DELETED = "music_remove_deleted"  # "true"/"false"
SYNC_ON_CONNECT = "sync_on_connect"            # "true"/"false"


async def get(db: AsyncSession, key: str, default: str | None = None) -> str | None:
    row = await db.scalar(select(Setting).where(Setting.key == key))
    if row is None:
        return default
    return row.value


async def set_(db: AsyncSession, key: str, value: str | None) -> None:
    row = await db.scalar(select(Setting).where(Setting.key == key))
    if row is None:
        row = Setting(key=key, value=value)
        db.add(row)
    else:
        row.value = value
    await db.commit()


async def get_all(db: AsyncSession) -> dict[str, str | None]:
    rows = (await db.scalars(select(Setting))).all()
    return {r.key: r.value for r in rows}


async def set_many(db: AsyncSession, data: dict[str, str | None]) -> None:
    for key, value in data.items():
        await set_(db, key, value)
