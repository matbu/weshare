"""Near-live snapshot proxy.

Whatever the number of viewers, a webcam source receives at most one request per
interval, and a conditional one (ETag / If-Modified-Since) when possible, so a source that
did not change only answers 304. The interval adapts to the camera: MIN_INTERVAL while its
picture keeps changing, doubling up to MAX_INTERVAL while it does not (most webcams publish
every 1-10 min), back to MIN_INTERVAL as soon as it changes. Only webcams someone is
looking at are fetched. If the source fails, the last good image keeps being served (its age is exposed
in X-Image-Updated so clients can show it).
"""

import asyncio
import contextlib
import email.utils
import hashlib
import time
from collections import OrderedDict
from dataclasses import dataclass, replace
from datetime import datetime, timezone

import httpx
from fastapi import HTTPException
from webcam_policy import is_blocked, is_commercial
from webcam_resolvers import resolve

from .config import settings
from .db import get_pool
from .media import BlockedURL, assert_public_url

MIN_INTERVAL = 3.0    # fastest polling of a source whose picture keeps changing
MAX_INTERVAL = 60.0   # slowest polling of a source whose picture does not change
RESOLVE_EVERY = 60.0  # seconds between two reads of a provider page (resolver endpoints)
ERROR_TTL = 60.0      # a failing source is retried at most once a minute
MAX_ENTRIES = 1000
MAX_BYTES = 8_000_000

_http = httpx.AsyncClient(
    headers={"User-Agent": settings.user_agent},
    timeout=httpx.Timeout(10.0, connect=5.0),
    follow_redirects=False,
)
# IP cameras often have self-signed certificates; we only read public images with it.
_http_unverified = httpx.AsyncClient(
    headers={"User-Agent": settings.user_agent},
    timeout=httpx.Timeout(10.0, connect=5.0),
    follow_redirects=False,
    verify=False,
)


@contextlib.asynccontextmanager
async def _stream(url: str, headers: dict | None):
    async with contextlib.AsyncExitStack() as stack:
        try:
            resp = await stack.enter_async_context(_http.stream("GET", url, headers=headers))
        except httpx.ConnectError as exc:
            if "CERTIFICATE_VERIFY_FAILED" not in str(exc):
                raise
            resp = await stack.enter_async_context(_http_unverified.stream("GET", url, headers=headers))
        yield resp


@dataclass(frozen=True)
class Snapshot:
    content_type: str
    body: bytes
    digest: str
    updated_at: datetime            # when the picture last changed
    checked_at: float               # monotonic time of the last request to the source
    image_url: str
    resolved_at: float
    source_etag: str | None = None
    source_last_modified: str | None = None
    interval: float = MIN_INTERVAL  # seconds until the source may be asked again


@dataclass(frozen=True)
class Failure:
    error: HTTPException
    checked_at: float


_cache: "OrderedDict[int, Snapshot | Failure]" = OrderedDict()
_locks: dict[int, asyncio.Lock] = {}


_blocked: tuple[float, frozenset[str]] = (0.0, frozenset())


async def blocked_hosts() -> frozenset[str]:
    """Hosts whose owners opted out (cached one minute)."""
    global _blocked
    if time.monotonic() - _blocked[0] > 60:
        rows = await get_pool().fetch("SELECT host FROM blocked_hosts")
        _blocked = (time.monotonic(), frozenset(r["host"] for r in rows))
    return _blocked[1]


def _fresh(entry: "Snapshot | Failure | None") -> bool:
    if entry is None:
        return False
    ttl = ERROR_TTL if isinstance(entry, Failure) else entry.interval
    return time.monotonic() - entry.checked_at < ttl


def _remember(webcam_id: int, entry: "Snapshot | Failure") -> None:
    _cache[webcam_id] = entry
    _cache.move_to_end(webcam_id)
    while len(_cache) > MAX_ENTRIES:
        _cache.popitem(last=False)


async def get(webcam_id: int) -> Snapshot:
    entry = _cache.get(webcam_id)
    if not _fresh(entry):
        lock = _locks.setdefault(webcam_id, asyncio.Lock())
        try:
            async with lock:
                entry = _cache.get(webcam_id)
                if not _fresh(entry):
                    entry = await _refresh(webcam_id, entry)
                    _remember(webcam_id, entry)
        finally:
            if not lock.locked():
                _locks.pop(webcam_id, None)
    if isinstance(entry, Failure):
        raise entry.error
    return entry


async def _fetch(url: str, headers: dict | None = None) -> tuple[int, httpx.Headers, bytes]:
    for _ in range(5):
        await assert_public_url(url)
        async with _stream(url, headers) as resp:
            if resp.is_redirect and resp.headers.get("location"):
                url = str(resp.url.join(resp.headers["location"]))
                continue
            body = b""
            if resp.status_code == 200:
                async for chunk in resp.aiter_bytes():
                    body += chunk
                    if len(body) > MAX_BYTES:
                        raise HTTPException(502, "image too large")
            return resp.status_code, resp.headers, body
    raise HTTPException(502, "too many redirects")


def _slower(prev: Snapshot) -> float:
    return min(prev.interval * 2, MAX_INTERVAL)


def _parse_http_date(value: str | None) -> datetime | None:
    try:
        return email.utils.parsedate_to_datetime(value) if value else None
    except (TypeError, ValueError):
        return None


async def _refresh(webcam_id: int, previous: "Snapshot | Failure | None") -> "Snapshot | Failure":
    prev = previous if isinstance(previous, Snapshot) else None
    now = time.monotonic()
    try:
        row = await get_pool().fetchrow(
            """
            SELECT e.url, e.resolver FROM webcam_endpoints e JOIN webcams w ON w.id = e.webcam_id
            WHERE e.webcam_id = $1 AND e.type = 'image' AND w.status <> 'rejected' AND w.source_enabled
              AND NOT EXISTS (SELECT 1 FROM sources x WHERE x.name = e.origin AND NOT x.enabled)
            ORDER BY (e.id = w.preview_endpoint_id) IS TRUE DESC, e.is_working DESC NULLS LAST
            LIMIT 1
            """,
            webcam_id,
        )
        if row is None:
            return Failure(HTTPException(404, "no image for this webcam"), now)
        if is_commercial(row["url"]):
            # Commercial providers sell these images: we never re-serve them (see webcam_policy).
            return Failure(HTTPException(403, "images of this provider are not proxied"), now)
        if is_blocked(row["url"], await blocked_hosts()):
            return Failure(HTTPException(410, "removed at the owner's request"), now)

        image_url, resolved_at = row["url"], now
        if row["resolver"]:
            if prev and now - prev.resolved_at < RESOLVE_EVERY:
                image_url, resolved_at = prev.image_url, prev.resolved_at
            else:
                status, _, page = await _fetch(row["url"])
                image_url = resolve(row["resolver"], page.decode("utf-8", "replace"), row["url"]) if status == 200 else None
                if not image_url:
                    raise HTTPException(502, f"{row['resolver']}: no current image")

        conditional = {}
        if prev and prev.image_url == image_url:
            if prev.source_etag:
                conditional["If-None-Match"] = prev.source_etag
            if prev.source_last_modified:
                conditional["If-Modified-Since"] = prev.source_last_modified
        status, headers, body = await _fetch(image_url, conditional)

        if status == 304 and prev:
            return replace(prev, checked_at=now, resolved_at=resolved_at, interval=_slower(prev))
        if status != 200:
            raise HTTPException(502, f"source returned HTTP {status}")
        content_type = headers.get("content-type", "").split(";")[0]
        if not content_type.startswith("image/"):
            raise HTTPException(502, "source did not return an image")

        digest = hashlib.sha1(body).hexdigest()
        if prev and prev.digest == digest:
            updated_at, interval = prev.updated_at, _slower(prev)
        else:
            updated_at = _parse_http_date(headers.get("last-modified")) or datetime.now(timezone.utc)
            interval = MIN_INTERVAL
        return Snapshot(content_type, body, digest, updated_at, now, image_url, resolved_at,
                        headers.get("etag"), headers.get("last-modified"), interval)
    except (HTTPException, BlockedURL, httpx.HTTPError) as exc:
        if prev:  # keep serving the last good picture; retry the source in ERROR_TTL
            return replace(prev, checked_at=now, interval=ERROR_TTL)
        if isinstance(exc, HTTPException):
            return Failure(exc, now)
        detail = str(exc) if isinstance(exc, BlockedURL) else f"source unreachable: {type(exc).__name__}"
        return Failure(HTTPException(502, detail), now)
