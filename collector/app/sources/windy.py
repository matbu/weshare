"""Windy Webcams API v3 (enabled only when WINDY_API_KEY is set).

Free tier: max 50 results per page and offset <= 1000, so listing is done by bbox and a
bbox holding more than 1000 webcams is split in four. Image URLs returned by Windy
expire (10 min free / 24 h pro): we never store them, only the stable player embed.
Check Windy's terms of use before exposing this data publicly.
"""

import json
import logging

from .. import db, store
from ..config import settings
from ..polite import FetchError, PoliteClient
from ..runs import RunTracker
from ..store import Endpoint, WebcamRecord
from .osm import Bbox, split, world_tiles

log = logging.getLogger(__name__)

SOURCE = "windy"
API_URL = "https://api.windy.com/webcams/api/v3/webcams"
PAGE_SIZE = 50
MAX_OFFSET = 1000
MAX_SPLIT_DEPTH = 6


def extract(cam: dict) -> WebcamRecord | None:
    loc = cam.get("location") or {}
    lat, lon = loc.get("latitude"), loc.get("longitude")
    if lat is None or lon is None:
        return None
    player = cam.get("player") or {}
    urls = cam.get("urls") or {}
    endpoints = []
    for key in ("live", "day"):
        if isinstance(player.get(key), str) and player[key].startswith("http"):
            endpoints.append(Endpoint("iframe", player[key]))
            break
    return WebcamRecord(
        source=SOURCE,
        source_id=str(cam["webcamId"]),
        latitude=lat,
        longitude=lon,
        name=cam.get("title"),
        country_code=loc.get("country_code"),
        region=loc.get("region"),
        city=loc.get("city"),
        source_url=urls.get("detail"),
        webpage_url=urls.get("provider"),
        endpoints=endpoints,
        raw={k: cam.get(k) for k in ("webcamId", "title", "status", "categories", "urls")},
    )


async def _page(client: PoliteClient, bbox: Bbox, offset: int) -> dict:
    s, w, n, e = bbox
    resp = await client.fetch(
        API_URL,
        params={
            # v3 expects north_lat,east_lon,south_lat,west_lon
            "bbox": f"{n},{e},{s},{w}",
            "limit": PAGE_SIZE,
            "offset": offset,
            "include": "categories,location,player,urls",
        },
        headers={"X-WINDY-API-KEY": settings.windy_api_key},
        check_public=False,
    )
    if resp.status != 200:
        raise FetchError(f"windy HTTP {resp.status}: {resp.body[:200]!r}")
    return json.loads(resp.body)


async def _collect_bbox(client, conn, tracker: RunTracker, bbox: Bbox, depth: int = 0) -> None:
    try:
        first = await _page(client, bbox, 0)
    except FetchError as exc:
        log.error("windy: %s failed: %s", bbox, exc)
        tracker.incr("errors")
        return
    total = first.get("total", 0)
    if total > MAX_OFFSET + PAGE_SIZE and depth < MAX_SPLIT_DEPTH:
        for sub in split(bbox):
            await _collect_bbox(client, conn, tracker, sub, depth + 1)
        return

    pages = [first]
    offset = PAGE_SIZE
    while offset < min(total, MAX_OFFSET + PAGE_SIZE):
        try:
            pages.append(await _page(client, bbox, offset))
        except FetchError as exc:
            log.error("windy: %s offset %d failed: %s", bbox, offset, exc)
            tracker.incr("errors")
            break
        offset += PAGE_SIZE

    records = [r for p in pages for r in (extract(c) for c in p.get("webcams", [])) if r]
    tracker.incr("discovered", len(records))
    async with conn.transaction():
        for rec in records:
            outcome = await store.upsert(conn, rec)
            tracker.incr({"inserted": "inserted", "updated": "updated"}.get(outcome, "duplicates"))


async def run(client: PoliteClient, tracker: RunTracker, bbox: Bbox | None = None) -> None:
    if not settings.windy_api_key:
        log.info("windy: WINDY_API_KEY not set, skipping")
        return
    client.set_host_interval("api.windy.com", 1.5)
    async with await db.connect(autocommit=True) as conn:
        for tile in [bbox] if bbox else world_tiles():
            await _collect_bbox(client, conn, tracker, tile)
        if bbox is None and tracker.counts["errors"] == 0:
            async with conn.transaction():
                tracker.incr("removed", await store.delete_stale_sources(conn, SOURCE, tracker.started_at))
