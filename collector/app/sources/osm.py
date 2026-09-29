"""OpenStreetMap webcams through Overpass.

Instead of one giant world query (which times out and hammers public instances),
the world is cut into tiles queried one after the other; a tile that is too heavy is
split in four. Before every query we ask /api/status for a free slot, as the
Overpass usage policy requests.
"""

import asyncio
import json
import logging
import re
from urllib.parse import urlencode, urlsplit

from .. import db, geo, store
from ..config import settings
from ..media import classify_url, split_tag_urls
from ..polite import FetchError, PoliteClient
from ..runs import RunTracker
from ..store import Endpoint, WebcamRecord

log = logging.getLogger(__name__)

SOURCE = "osm"
TILE_DEG = 30
MAX_SPLIT_DEPTH = 4
QUERY_TIMEOUT = 180
RETRY_PASSES = 2
RETRY_PAUSE = 300

QUERY = """[out:json][timeout:{timeout}][bbox:{s},{w},{n},{e}];
(
  nwr["surveillance"="webcam"];
  nwr["surveillance:type"="webcam"];
  nwr["contact:webcam"];
);
out center tags;"""

URL_TAGS = ("contact:webcam", "webcam", "camera:url", "url", "website")

# Sites that OSM mappers put in contact:webcam although they are not cameras
# (wind beacons, weather stations without camera).
NOT_WEBCAM_HOSTS = ("pioupiou.com", "balisemeteo.com")
NAME_TAGS = ("name", "name:en", "operator", "description")

SLOT_NOW = re.compile(r"(\d+) slots? available now")
SLOT_AFTER = re.compile(r"in (\d+) seconds")

Bbox = tuple[float, float, float, float]  # south, west, north, east


class TileTooHeavy(Exception):
    pass


def world_tiles(step: int = TILE_DEG) -> list[Bbox]:
    return [
        (lat, lon, lat + step, lon + step)
        for lat in range(-90, 90, step)
        for lon in range(-180, 180, step)
    ]


def split(b: Bbox) -> list[Bbox]:
    s, w, n, e = b
    mlat, mlon = (s + n) / 2, (w + e) / 2
    return [(s, w, mlat, mlon), (s, mlon, mlat, e), (mlat, w, n, mlon), (mlat, mlon, n, e)]


def extract(element: dict) -> WebcamRecord | None:
    tags = element.get("tags") or {}
    if element["type"] == "node":
        lat, lon = element.get("lat"), element.get("lon")
    else:
        center = element.get("center") or {}
        lat, lon = center.get("lat"), center.get("lon")
    if lat is None or lon is None:
        return None

    is_webcam = tags.get("surveillance") == "webcam" or tags.get("surveillance:type") == "webcam"
    # Weather / wind stations whose contact:webcam points to their data page.
    if tags.get("man_made") == "monitoring_station" and not any(k.startswith("surveillance") for k in tags):
        return None

    urls: list[str] = []
    for tag in URL_TAGS:
        # url/website on a generic POI (hotel, ski resort) is not the camera itself.
        if tag in ("url", "website") and not is_webcam:
            continue
        for url in split_tag_urls(tags.get(tag)):
            if url not in urls and not _not_webcam_host(url):
                urls.append(url)
    if not urls and not is_webcam:
        return None

    name = next((tags[t] for t in NAME_TAGS if tags.get(t)), None)
    webcam_page = next((u for u in urls if classify_url(u) == "page"), None)

    return WebcamRecord(
        source=SOURCE,
        source_id=f"{element['type']}/{element['id']}",
        latitude=lat,
        longitude=lon,
        name=name,
        description=tags.get("description"),
        country_code=tags.get("addr:country") if len(tags.get("addr:country", "")) == 2 else None,
        city=tags.get("addr:city"),
        source_url=f"https://www.openstreetmap.org/{element['type']}/{element['id']}",
        webpage_url=webcam_page,
        endpoints=[Endpoint(classify_url(u), u) for u in urls],
        raw=tags,
    )


def _not_webcam_host(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in NOT_WEBCAM_HOSTS)


async def drop_filtered(conn) -> int:
    """Re-apply extract() to stored OSM tags, so a new filter also cleans existing rows."""
    cur = await conn.execute(
        "SELECT id, source_id, raw FROM webcam_sources WHERE source = %s AND raw IS NOT NULL", (SOURCE,)
    )
    rejected = []
    async for row in cur:
        osm_type, _, osm_id = row["source_id"].partition("/")
        element = {"type": osm_type, "id": osm_id, "lat": 0.0, "lon": 0.0,
                   "center": {"lat": 0.0, "lon": 0.0}, "tags": row["raw"]}
        if extract(element) is None:
            rejected.append(row["id"])
    if rejected:
        async with conn.transaction():
            await conn.execute("DELETE FROM webcam_sources WHERE id = ANY(%s)", (rejected,))
            await store.delete_orphans(conn)
    return len(rejected)


def _status_url(interpreter_url: str) -> str:
    return interpreter_url.rsplit("/", 1)[0] + "/status"


async def _wait_for_slot(client: PoliteClient, base: str) -> None:
    for _ in range(20):
        try:
            resp = await client.fetch(_status_url(base), retries=0, check_public=False)
        except FetchError:
            return  # some mirrors don't expose /status
        text = resp.body.decode("utf-8", "replace")
        if resp.status != 200 or SLOT_NOW.search(text):
            return
        waits = [int(x) for x in SLOT_AFTER.findall(text)]
        delay = min(waits) + 2 if waits else 15
        log.info("overpass: no free slot on %s, waiting %ss", urlsplit(base).hostname, delay)
        await asyncio.sleep(delay)


async def _query(client: PoliteClient, bbox: Bbox) -> list[dict]:
    s, w, n, e = bbox
    query = QUERY.format(timeout=QUERY_TIMEOUT, s=s, w=w, n=n, e=e)
    last_error: Exception | None = None
    for base in settings.overpass_urls:
        await _wait_for_slot(client, base)
        try:
            resp = await client.fetch(
                base,
                method="POST",
                data=urlencode({"data": query}),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                max_bytes=512_000_000,
                check_public=False,
                timeout=QUERY_TIMEOUT + 60,
                retries=2,
            )
        except FetchError as exc:
            log.warning("overpass: %s failed: %s", urlsplit(base).hostname, exc)
            last_error = exc
            continue
        # 429/504 mean "server busy" (already retried with backoff): try the next mirror.
        # A query that is really too heavy returns 200 with a runtime error remark.
        tail = resp.body[-2000:]
        if resp.status == 200 and b'"remark"' in tail and b"runtime error" in tail:
            raise TileTooHeavy(bbox)
        if resp.status != 200:
            log.warning("overpass: %s answered HTTP %d", urlsplit(base).hostname, resp.status)
            last_error = FetchError(f"overpass HTTP {resp.status}")
            continue
        return json.loads(resp.body)["elements"]
    raise last_error or FetchError("no overpass endpoint configured")


async def _collect_tile(
    client, conn, tracker: RunTracker, bbox: Bbox, failed: list[Bbox], depth: int = 0
) -> None:
    try:
        elements = await _query(client, bbox)
    except TileTooHeavy:
        if depth >= MAX_SPLIT_DEPTH:
            log.error("overpass: tile %s still too heavy at max depth", bbox)
            tracker.incr("errors")
            return
        log.info("overpass: splitting %s", bbox)
        for sub in split(bbox):
            await _collect_tile(client, conn, tracker, sub, failed, depth + 1)
        return
    except FetchError as exc:
        log.warning("overpass: tile %s failed, will retry at the end: %s", bbox, exc)
        failed.append(bbox)
        return

    records = [r for r in (extract(el) for el in elements) if r]
    tracker.incr("discovered", len(records))
    await geo.enrich(records)
    async with conn.transaction():
        for rec in records:
            outcome = await store.upsert(conn, rec)
            tracker.incr({"inserted": "inserted", "updated": "updated"}.get(outcome, "duplicates"))
    log.info("overpass: tile %s -> %d webcams", bbox, len(records))


async def run(client: PoliteClient, tracker: RunTracker, bbox: Bbox | None = None) -> None:
    for base in settings.overpass_urls:
        client.set_host_interval(urlsplit(base).hostname or "", settings.overpass_interval)

    tiles = [bbox] if bbox else world_tiles()
    async with await db.connect(autocommit=True) as conn:
        tracker.incr("filtered", await drop_filtered(conn))
        failed: list[Bbox] = []
        for tile in tiles:
            await _collect_tile(client, conn, tracker, tile, failed)

        # Busy public instances: give them a break, then one more pass on failed tiles.
        for attempt in range(RETRY_PASSES):
            if not failed:
                break
            log.info("overpass: %d tiles to retry in %ds", len(failed), RETRY_PAUSE)
            await asyncio.sleep(RETRY_PAUSE)
            retry, failed = failed, []
            for tile in retry:
                await _collect_tile(client, conn, tracker, tile, failed)
        if failed:
            log.error("overpass: %d tiles failed: %s", len(failed), failed)
            tracker.incr("errors", len(failed))

        # Only a complete, error-free world run may conclude that a webcam disappeared.
        if bbox is None and tracker.counts["errors"] == 0:
            async with conn.transaction():
                removed = await store.delete_stale_sources(conn, SOURCE, tracker.started_at)
            tracker.incr("removed", removed)
