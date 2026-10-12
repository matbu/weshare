"""California road cameras (Caltrans).

Public-domain open data, no API key. Each of the 12 districts publishes a JSON file with,
per camera, a still image URL and often a live HLS stream. We store both; the health
checker validates them and the cameras that are out of service drop out on their own.
https://cwwp2.dot.ca.gov/documentation/cctv/cctv.htm — data © Caltrans, public domain.
"""

import json
import logging

from .. import db, store
from ..polite import FetchError, PoliteClient
from ..runs import RunTracker
from ..store import Endpoint, WebcamRecord

log = logging.getLogger(__name__)

SOURCE = "caltrans"
DISTRICTS = range(1, 13)
URL = "https://cwwp2.dot.ca.gov/data/d{n}/cctv/cctvStatusD{n:02d}.json"


def extract(district: int, entry: dict) -> WebcamRecord | None:
    cam = entry.get("cctv") or {}
    loc = cam.get("location") or {}
    index = cam.get("index")
    try:
        lat, lon = float(loc.get("latitude") or "0"), float(loc.get("longitude") or "0")
    except ValueError:
        return None
    if not index or lat == 0 or lon == 0:
        return None

    image = ((cam.get("imageData") or {}).get("static") or {}).get("currentImageURL")
    stream = (cam.get("imageData") or {}).get("streamingVideoURL")
    endpoints = []
    if image and image.startswith("http"):
        endpoints.append(Endpoint("image", image))
    if stream and stream.startswith("http") and ".m3u8" in stream:
        endpoints.append(Endpoint("hls", stream))
    if not endpoints:
        return None

    return WebcamRecord(
        source=SOURCE,
        source_id=f"d{district}/{index}",
        latitude=lat,
        longitude=lon,
        name=loc.get("locationName") or None,
        country_code="US",
        region="California",
        city=loc.get("nearbyPlace") or loc.get("county") or None,
        source_url="https://cwwp2.dot.ca.gov/closed-circuit-television-cameras.html",
        endpoints=endpoints,
        raw={"district": str(district), "route": loc.get("route"), "county": loc.get("county"),
             "inService": cam.get("inService")},
    )


async def _fetch_district(client: PoliteClient, district: int) -> list[dict]:
    resp = await client.fetch(URL.format(n=district), max_bytes=20_000_000, retries=1, check_public=False)
    if resp.status == 404:
        return []  # not every district publishes a file
    if resp.status != 200:
        raise FetchError(f"HTTP {resp.status}")
    return (json.loads(resp.body) or {}).get("data") or []


async def run(client: PoliteClient, tracker: RunTracker) -> None:
    async with await db.connect(autocommit=True) as conn:
        if not await store.source_enabled(conn, SOURCE):
            log.info("caltrans: source disabled by a moderator, skipping")
            tracker.incr("skipped_disabled")
            return

        records: list[WebcamRecord] = []
        for district in DISTRICTS:
            try:
                entries = await _fetch_district(client, district)
            except FetchError as exc:
                log.error("caltrans: district %d failed: %s", district, exc)
                tracker.incr("errors")
                continue
            found = [r for r in (extract(district, e) for e in entries) if r]
            records.extend(found)
            log.info("caltrans: district %d -> %d cameras", district, len(found))

        tracker.incr("discovered", len(records))
        async with conn.transaction():
            for rec in records:
                outcome = await store.upsert(conn, rec)
                tracker.incr({"inserted": "inserted", "updated": "updated", "blocked": "blocked"}
                             .get(outcome, "duplicates"))

        # Only a complete, error-free run may decide a camera disappeared.
        if tracker.counts["errors"] == 0:
            async with conn.transaction():
                tracker.incr("removed", await store.delete_stale_sources(conn, SOURCE, tracker.started_at))
