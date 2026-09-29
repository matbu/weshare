"""Checks that media endpoints are alive and still refreshing.

Every endpoint has its own next_check_at: healthy ones are re-checked every few hours,
failing ones with exponential backoff (so dead hosts receive almost no traffic).
Conditional GETs (ETag / If-Modified-Since) keep the load on webcam hosts minimal.

Streams (MJPEG, HLS, DASH, MP4) are validated by app.probe from what the URL really serves;
an endpoint whose content does not match its type is reclassified and re-checked at once.
"""

import asyncio
import email.utils
import hashlib
import io
import logging
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import psycopg
from PIL import Image
from webcam_resolvers import resolve

from .. import db, store
from ..config import settings
from ..media import sniff_image
from ..probe import looks_like_stream, probe, sniff
from ..polite import BlockedURL, FetchError, PoliteClient
from ..runs import RunTracker

log = logging.getLogger(__name__)

OK_INTERVAL = timedelta(hours=6)
FROZEN_AFTER = timedelta(hours=48)
FROZEN_RECHECK = timedelta(hours=12)
FAIL_BASE = timedelta(minutes=30)
FAIL_MAX = timedelta(days=7)

SELECT_DUE = """
SELECT id, webcam_id, type::text AS type, url, resolver, etag, last_modified, content_hash,
       last_change, fail_count
FROM webcam_endpoints
WHERE type IN ('image', 'mjpeg', 'hls', 'mp4', 'dash') AND next_check_at <= now()
ORDER BY next_check_at
LIMIT %s
"""

UPDATE = """
UPDATE webcam_endpoints SET
    is_working    = %(ok)s,
    live          = %(live)s,
    http_status   = %(status)s,
    last_error    = %(error)s,
    fail_count    = %(fail_count)s,
    last_checked  = now(),
    last_success  = CASE WHEN %(reachable)s THEN now() ELSE last_success END,
    next_check_at = %(next_check_at)s,
    width         = COALESCE(%(width)s, width),
    height        = COALESCE(%(height)s, height),
    etag          = COALESCE(%(etag)s, etag),
    last_modified = COALESCE(%(last_modified)s, last_modified),
    content_hash  = COALESCE(%(content_hash)s, content_hash),
    last_change   = %(last_change)s
WHERE id = %(id)s
"""


@dataclass
class Check:
    reachable: bool
    status: int | None = None
    error: str | None = None
    width: int | None = None
    height: int | None = None
    content_hash: str | None = None
    not_modified: bool = False
    etag: str | None = None
    last_modified: str | None = None
    live: bool | None = None
    kind: str | None = None  # what the URL really serves, when it differs from the endpoint type

    @property
    def source_updated(self) -> datetime | None:
        """When the source says the image last changed (Last-Modified), if it says so."""
        try:
            return email.utils.parsedate_to_datetime(self.last_modified) if self.last_modified else None
        except (TypeError, ValueError):
            return None


async def _resolve(client: PoliteClient, ep: dict) -> str | Check:
    """Current image URL of a provider page (see shared/webcam_resolvers.py)."""
    page = await client.fetch(ep["url"], max_bytes=2_000_000, retries=1)
    if page.status != 200:
        return Check(False, page.status, f"provider page HTTP {page.status}")
    image_url = resolve(ep["resolver"], page.body.decode("utf-8", "replace"), page.url)
    return image_url or Check(False, page.status, f"{ep['resolver']}: no image in page")


async def check_image(client: PoliteClient, ep: dict) -> Check:
    url, headers = ep["url"], {}
    if ep["resolver"]:
        url = await _resolve(client, ep)
        if isinstance(url, Check):
            return url
    else:  # conditional GET only makes sense on a stable URL
        if ep["etag"]:
            headers["If-None-Match"] = ep["etag"]
        if ep["last_modified"]:
            headers["If-Modified-Since"] = ep["last_modified"]
    resp = await client.fetch(url, headers=headers, max_bytes=10_000_000, retries=1, until=looks_like_stream)
    meta = {"status": resp.status, "etag": resp.headers.get("etag"),
            "last_modified": resp.headers.get("last-modified")}
    if resp.status == 304:
        return Check(True, not_modified=True, **meta)
    if resp.status != 200:
        return Check(False, error=f"HTTP {resp.status}", **meta)
    actual = sniff(resp.content_type, resp.body)
    if actual in STREAM_TYPES:
        return Check(False, error=f"is a {actual} stream", kind=actual, **meta)
    if resp.truncated:
        return Check(False, error="image too large", **meta)
    if not sniff_image(resp.body):
        return Check(False, error=f"not an image ({resp.content_type or 'unknown'})", **meta)
    try:
        with Image.open(io.BytesIO(resp.body)) as img:
            width, height = img.size
    except Exception:
        return Check(False, error="undecodable image", **meta)
    if width < 160 or height < 90:
        return Check(False, error=f"image too small ({width}x{height})", **meta)
    digest = hashlib.sha1(resp.body).hexdigest()
    return Check(True, width=width, height=height, content_hash=digest, **meta)


STREAM_TYPES = ("mjpeg", "hls", "dash", "mp4")


async def check_stream(client: PoliteClient, ep: dict) -> Check:
    found = await probe(client, ep["url"])
    if found.kind == ep["type"]:
        return Check(True, 200, live=found.live)
    if found.kind in STREAM_TYPES + ("image",):
        return Check(False, 200, f"is a {found.kind}", kind=found.kind)
    return Check(False, error=found.error)


CHECKERS = {"image": check_image, **{t: check_stream for t in STREAM_TYPES}}


def evaluate(ep: dict, check: Check, now: datetime) -> dict:
    """Turns a raw check into the new endpoint state (pure function, unit-tested)."""
    changed = check.content_hash is not None and check.content_hash != ep["content_hash"]
    first_success = ep["last_change"] is None and check.reachable
    last_change = now if changed or first_success else ep["last_change"]
    # Frozen: same picture for FROZEN_AFTER, or the source itself says it is that old.
    source_updated = check.source_updated
    frozen = ep["type"] == "image" and check.reachable and (
        (last_change is not None and now - last_change > FROZEN_AFTER)
        or (source_updated is not None and now - source_updated > FROZEN_AFTER)
    )
    ok = check.reachable and not frozen
    reclassified = check.kind is not None and check.kind != ep["type"]
    fail_count = 0 if check.reachable or reclassified else ep["fail_count"] + 1
    if reclassified:
        delay = timedelta(0)  # check it again right away with the right checker
    elif frozen:
        delay = FROZEN_RECHECK
    elif ok:
        delay = OK_INTERVAL
    else:
        delay = min(FAIL_MAX, FAIL_BASE * 2 ** min(fail_count - 1, 12))
    delay *= random.uniform(0.9, 1.2)  # spread checks so hosts never see bursts
    return {
        "id": ep["id"],
        "ok": ok,
        "live": check.live,
        "reachable": check.reachable,
        "status": check.status,
        "error": "frozen image" if frozen else check.error,
        "fail_count": fail_count,
        "next_check_at": now + delay,
        "width": check.width,
        "height": check.height,
        "etag": check.etag,
        "last_modified": check.last_modified,
        "content_hash": check.content_hash,
        "last_change": last_change,
    }


async def _check(client: PoliteClient, ep: dict) -> Check:
    try:
        return await CHECKERS[ep["type"]](client, ep)
    except BlockedURL as exc:
        return Check(False, error=f"blocked: {exc}")
    except FetchError as exc:
        return Check(False, error=str(exc)[:500])


async def run(client: PoliteClient, tracker: RunTracker) -> None:
    async with await db.connect(autocommit=True) as conn:
        cur = await conn.execute(SELECT_DUE, (settings.health_batch,))
        endpoints = await cur.fetchall()
        tracker.incr("discovered", len(endpoints))
        if not endpoints:
            return

        results = await asyncio.gather(*(_check(client, ep) for ep in endpoints))

        now = datetime.now(timezone.utc)
        async with conn.transaction():
            async with conn.cursor() as c:
                await c.executemany(UPDATE, [evaluate(ep, chk, now) for ep, chk in zip(endpoints, results)])
            await store.refresh_live(conn, [ep["webcam_id"] for ep in endpoints])

        # Outside the batch transaction: one conflicting URL must not roll back everything.
        for ep, chk in zip(endpoints, results):
            if chk.kind and chk.kind != ep["type"]:
                try:
                    await conn.execute(
                        "UPDATE webcam_endpoints SET type = %s WHERE id = %s", (chk.kind, ep["id"])
                    )
                    tracker.incr(f"reclassified_{ep['type']}_to_{chk.kind}")
                except psycopg.errors.UniqueViolation:
                    await conn.execute("DELETE FROM webcam_endpoints WHERE id = %s", (ep["id"],))
                    tracker.incr("duplicate_removed")

        tracker.incr("updated", len(endpoints))
        for chk in results:
            tracker.incr("alive" if chk.reachable else "dead")
