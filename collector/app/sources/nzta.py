"""New Zealand road cameras (NZ Transport Agency / Waka Kotahi).

Open data, no API key. Camera data is licensed CC BY 4.0 (commercial use allowed with
attribution): https://www.nzta.govt.nz/traffic-and-travel-information/use-our-data
One request returns every camera as XML; images update periodically and are proxied by us
(NZTA is a public agency, not a commercial provider). The health checker sorts out the
cameras that are offline / under maintenance.
"""

import logging
from urllib.parse import urljoin
from xml.etree import ElementTree

from .. import db, geo, store
from ..polite import FetchError, PoliteClient
from ..runs import RunTracker
from ..store import Endpoint, WebcamRecord

log = logging.getLogger(__name__)

SOURCE = "nzta"
BASE = "https://trafficnz.info"
CAMERAS_URL = f"{BASE}/service/traffic/rest/4/cameras/all"


def _text(cam: ElementTree.Element, tag: str) -> str | None:
    el = cam.find(tag)
    return el.text.strip() if el is not None and el.text else None


def extract(cam: ElementTree.Element) -> WebcamRecord | None:
    cam_id = _text(cam, "id")
    lat, lon, image = _text(cam, "latitude"), _text(cam, "longitude"), _text(cam, "imageUrl")
    if not (cam_id and lat and lon and image):
        return None
    try:
        latitude, longitude = float(lat), float(lon)
    except ValueError:
        return None

    region = cam.find("region")
    view = _text(cam, "viewUrl")
    return WebcamRecord(
        source=SOURCE,
        source_id=cam_id,
        latitude=latitude,
        longitude=longitude,
        name=_text(cam, "name"),
        description=_text(cam, "description"),
        country_code="NZ",
        region=region.findtext("name") if region is not None else None,
        source_url=urljoin(BASE, view) if view else f"{BASE}/camera/{cam_id}",
        endpoints=[Endpoint("image", urljoin(BASE, image))],
        raw={"highway": _text(cam, "highway"), "direction": _text(cam, "direction"),
             "offline": _text(cam, "offline")},
    )


async def run(client: PoliteClient, tracker: RunTracker) -> None:
    async with await db.connect(autocommit=True) as conn:
        if not await store.source_enabled(conn, SOURCE):
            log.info("nzta: source disabled by a moderator, skipping")
            tracker.incr("skipped_disabled")
            return
        try:
            resp = await client.fetch(CAMERAS_URL, max_bytes=20_000_000, retries=2, check_public=False)
        except FetchError as exc:
            log.error("nzta: fetch failed: %s", exc)
            tracker.incr("errors")
            return
        if resp.status != 200:
            log.error("nzta: HTTP %s", resp.status)
            tracker.incr("errors")
            return
        try:
            root = ElementTree.fromstring(resp.body)
        except ElementTree.ParseError as exc:
            log.error("nzta: invalid XML: %s", exc)
            tracker.incr("errors")
            return

        records = [r for r in (extract(c) for c in root.iter("camera")) if r]
        tracker.incr("discovered", len(records))
        await geo.enrich(records)
        async with conn.transaction():
            for rec in records:
                outcome = await store.upsert(conn, rec)
                tracker.incr({"inserted": "inserted", "updated": "updated", "blocked": "blocked"}
                             .get(outcome, "duplicates"))
        log.info("nzta: %d cameras", len(records))

        if tracker.counts["errors"] == 0:
            async with conn.transaction():
                tracker.incr("removed", await store.delete_stale_sources(conn, SOURCE, tracker.started_at))
