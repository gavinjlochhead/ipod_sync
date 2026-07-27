"""
FastAPI application entry point.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.database import init_db, AsyncSessionLocal
from app.routes import web, api
from app.services import mqtt_pub
from app.services import gpio_control
from app import config as cfg

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    log.info("Database initialised")

    # Re-configure MQTT if previously saved
    async with AsyncSessionLocal() as db:
        s = await cfg.get_all(db)
    mqtt_host = s.get(cfg.MQTT_HOST)
    if mqtt_host:
        mqtt_pub.configure(
            host=mqtt_host,
            port=int(s.get(cfg.MQTT_PORT, "1883") or "1883"),
            username=s.get(cfg.MQTT_USERNAME) or None,
            password=s.get(cfg.MQTT_PASSWORD) or None,
            prefix=s.get(cfg.MQTT_PREFIX, "ipod_sync") or "ipod_sync",
            ha_discovery=(s.get(cfg.MQTT_HA_DISCOVERY, "true") == "true"),
            api_base="http://127.0.0.1:8000",
        )
        if s.get(cfg.MQTT_HA_DISCOVERY, "true") == "true":
            mqtt_pub.publish_ha_discovery()
        log.info("MQTT configured from saved settings")

    # Re-configure GPIO buttons/LEDs if previously saved
    def _int_or_none(v: str | None) -> int | None:
        try:
            return int(v) if v not in (None, "") else None
        except ValueError:
            return None

    gpio_control.configure(
        enabled=s.get(cfg.GPIO_ENABLED, "false") == "true",
        mount_pin=_int_or_none(s.get(cfg.GPIO_BUTTON_MOUNT_PIN)),
        unmount_pin=_int_or_none(s.get(cfg.GPIO_BUTTON_UNMOUNT_PIN)),
        sync_pin=_int_or_none(s.get(cfg.GPIO_BUTTON_SYNC_PIN)),
        led_mounted_pin=_int_or_none(s.get(cfg.GPIO_LED_MOUNTED_PIN)),
        led_removable_pin=_int_or_none(s.get(cfg.GPIO_LED_REMOVABLE_PIN)),
        led_syncing_pin=_int_or_none(s.get(cfg.GPIO_LED_SYNCING_PIN)),
        mount_point=s.get(cfg.IPOD_MOUNT) or "",
        ipod_label=s.get(cfg.IPOD_LABEL, "IPOD") or "IPOD",
        session_factory=AsyncSessionLocal,
    )
    if s.get(cfg.GPIO_ENABLED, "false") == "true":
        log.info("GPIO configured from saved settings")

    yield


app = FastAPI(title="iPod Sync", lifespan=lifespan)

import os
_static = os.path.join(os.path.dirname(__file__), "static")
if os.path.isdir(_static):
    app.mount("/static", StaticFiles(directory=_static), name="static")

app.include_router(web.router)
app.include_router(api.router, prefix="/api")
