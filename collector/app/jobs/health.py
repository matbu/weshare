"""Checks that media endpoints are alive and still refreshing.

Every endpoint has its own next_check_at: healthy ones are re-checked every few hours,
failing ones with exponential backoff (so dead hosts receive almost no traffic).
Conditional GETs (ETag / If-Modified-Since) keep the load on webcam hosts minimal.
"""

import asyncio
import hashlib
import io
import logging
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from PIL import Image

from .. import db, store
from ..config import settings
from ..media import sniff_image
from ..polite import BlockedURL, FetchError, PoliteClient
from ..runs import RunTracker

log = logging.getLogger(__name__)

OK_INTERVAL = timedelta(hours=6)
FROZEN_AFTER = timedelta(hours=48)
FROZEN_RECHECK = timedelta(hours=12)
FAIL_BASE = timedelta(minutes=30)
FAIL_MAX = timedelta(days=7)

SELECT_DUE = """
SELECT id, webcam_id, type::text AS type, url, etag, last_modified, content_hash,
       last_change, fail_count
FROM webcam_endpoints
WHERE type IN ('image', 'mjpeg', 'hls') AND next_check_at <= now()
ORDER BY next_check_at
LIMIT %s
"""

UPDATE = """
UPDATE webcam_endpoints SET
    is_working    = %(ok)s,
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


async def check_image(client: PoliteClient, ep: dict) -> Check:
    headers = {}
    if ep["etag"]:
        headers["If-None-Match"] = ep["etag"]
    if ep["last_modified"]:
        headers["If-Modified-Since"] = ep["last_modified"]
    resp = await client.fetch(ep["url"], headers=headers, max_bytes=10_000_000, retries=1)
    meta = {"status": resp.status, "etag": resp.headers.get("etag"),
            "last_modified": resp.headers.get("last-modified")}
    if resp.status == 304:
        return Check(True, not_modified=True, **meta)
    if resp.status != 200:
        return Check(False, error=f"HTTP {resp.status}", **meta)
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


async def check_mjpeg(client: PoliteClient, ep: dict) -> Check:
    resp = await client.fetch(ep["url"], max_bytes=300_000, retries=1, timeout=20)
    if resp.status != 200:
        return Check(False, resp.status, f"HTTP {resp.status}")
    if resp.content_type.startswith("multipart/") or b"\xff\xd8\xff" in resp.body:
        return Check(True, resp.status)
    return Check(False, resp.status, f"not a MJPEG stream ({resp.content_type})")


async def check_hls(client: PoliteClient, ep: dict) -> Check:
    resp = await client.fetch(ep["url"], max_bytes=1_000_000, retries=1)
    if resp.status != 200:
        return Check(False, resp.status, f"HTTP {resp.status}")
    if resp.body.lstrip().startswith(b"#EXTM3U"):
        return Check(True, resp.status)
    return Check(False, resp.status, "not a HLS playlist")


CHECKERS = {"image": check_image, "mjpeg": check_mjpeg, "hls": check_hls}


def evaluate(ep: dict, check: Check, now: datetime) -> dict:
    """Turns a raw check into the new endpoint state (pure function, unit-tested)."""
    changed = check.content_hash is not None and check.content_hash != ep["content_hash"]
    first_success = ep["last_change"] is None and check.reachable
    last_change = now if changed or first_success else ep["last_change"]
    frozen = (
        ep["type"] == "image"
        and check.reachable
        and last_change is not None
        and now - last_change > FROZEN_AFTER
    )
    ok = check.reachable and not frozen
    fail_count = 0 if check.reachable else ep["fail_count"] + 1
    if frozen:
        delay = FROZEN_RECHECK
    elif ok:
        delay = OK_INTERVAL
    else:
        delay = min(FAIL_MAX, FAIL_BASE * 2 ** min(fail_count - 1, 12))
    delay *= random.uniform(0.9, 1.2)  # spread checks so hosts never see bursts
    return {
        "id": ep["id"],
        "ok": ok,
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

        tracker.incr("updated", len(endpoints))
        for chk in results:
            tracker.incr("alive" if chk.reachable else "dead")
