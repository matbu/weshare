import asyncio
import random

import httpx
from cachetools import TTLCache
from fastapi import APIRouter, Depends, HTTPException, Query, Response

from ..config import settings
from ..db import get_pool
from ..media import BlockedURL, assert_public_url
from ..schemas import WebcamDetail, WebcamSummary
from ..security import optional_user

router = APIRouter(prefix="/webcams", tags=["webcams"])

SUMMARY = """
    w.id, w.name, w.latitude, w.longitude, w.country_code, w.region, w.city,
    w.is_live, w.status::text AS status,
    pe.type::text AS preview_type, pe.url AS preview_url
"""
PREVIEW_JOIN = "LEFT JOIN webcam_endpoints pe ON pe.id = w.preview_endpoint_id"


@router.get("/random", response_model=list[WebcamSummary])
async def random_webcams(
    count: int = Query(10, ge=1, le=50),
    country: str | None = Query(None, min_length=2, max_length=2),
):
    """Swipe feed: random live webcams, served from the partial (rand) index."""
    pivot = random.random()
    feed = f"""
        SELECT {SUMMARY} FROM webcams w {PREVIEW_JOIN}
        WHERE w.is_live AND w.status = 'approved'
          AND ($3::text IS NULL OR w.country_code = $3)
          AND w.rand {{op}} $1
        ORDER BY w.rand LIMIT $2
    """
    rows = await get_pool().fetch(
        f"({feed.format(op='>=')}) UNION ALL ({feed.format(op='<')}) LIMIT $2",
        pivot,
        count,
        country.upper() if country else None,
    )
    rows = list(rows)
    random.shuffle(rows)
    return [WebcamSummary.from_row(r) for r in rows]


@router.get("/nearby", response_model=list[WebcamSummary])
async def nearby(
    lat: float = Query(..., ge=-90, le=90),
    lon: float = Query(..., ge=-180, le=180),
    radius: float = Query(10_000, gt=0, le=300_000, description="meters"),
    limit: int = Query(100, ge=1, le=500),
    live_only: bool = False,
):
    rows = await get_pool().fetch(
        f"""
        WITH p AS (SELECT ST_SetSRID(ST_MakePoint($2, $1), 4326)::geography AS g)
        SELECT {SUMMARY}, ST_Distance(w.geom::geography, p.g) AS distance_m
        FROM p, webcams w {PREVIEW_JOIN}
        WHERE w.status = 'approved' AND (NOT $5 OR w.is_live)
          AND ST_DWithin(w.geom::geography, p.g, $3)
        ORDER BY w.geom::geography <-> p.g
        LIMIT $4
        """,
        lat, lon, radius, limit, live_only,
    )
    return [WebcamSummary.from_row(r) for r in rows]


@router.get("/bbox", response_model=list[WebcamSummary])
async def in_bbox(
    min_lon: float = Query(..., ge=-180, le=180),
    min_lat: float = Query(..., ge=-90, le=90),
    max_lon: float = Query(..., ge=-180, le=180),
    max_lat: float = Query(..., ge=-90, le=90),
    limit: int = Query(500, ge=1, le=2000),
    live_only: bool = False,
):
    rows = await get_pool().fetch(
        f"""
        SELECT {SUMMARY} FROM webcams w {PREVIEW_JOIN}
        WHERE w.status = 'approved' AND (NOT $6 OR w.is_live)
          AND w.geom && ST_MakeEnvelope($1, $2, $3, $4, 4326)
        ORDER BY w.is_live DESC, w.rand
        LIMIT $5
        """,
        min_lon, min_lat, max_lon, max_lat, limit, live_only,
    )
    return [WebcamSummary.from_row(r) for r in rows]


@router.get("/{webcam_id}", response_model=WebcamDetail)
async def get_webcam(webcam_id: int, user: dict | None = Depends(optional_user)):
    pool = get_pool()
    row = await pool.fetchrow(
        f"""
        SELECT {SUMMARY}, w.description, w.first_seen, w.submitted_by
        FROM webcams w {PREVIEW_JOIN} WHERE w.id = $1
        """,
        webcam_id,
    )
    if row is None:
        raise HTTPException(404, "webcam not found")
    mine = user is not None and row["submitted_by"] == user["id"]
    is_mod = user is not None and user["role"] in ("moderator", "admin")
    if row["status"] != "approved" and not (mine or is_mod):
        raise HTTPException(404, "webcam not found")

    endpoints = await pool.fetch(
        "SELECT id, type::text AS type, url, origin, is_working, width, height, last_success "
        "FROM webcam_endpoints WHERE webcam_id = $1 ORDER BY is_working DESC NULLS LAST, id",
        webcam_id,
    )
    sources = await pool.fetch(
        "SELECT source, source_id, source_url, webpage_url FROM webcam_sources "
        "WHERE webcam_id = $1 ORDER BY id",
        webcam_id,
    )
    summary = WebcamSummary.from_row(row)
    return WebcamDetail(
        **summary.model_dump(),
        description=row["description"],
        first_seen=row["first_seen"],
        endpoints=[dict(e) for e in endpoints],
        sources=[dict(s) for s in sources],
        submitted_by_me=mine,
    )


# ---------------------------------------------------------------------------
# Snapshot proxy: each webcam image is fetched at most once per minute from its
# source, whatever the number of viewers.
# ---------------------------------------------------------------------------

SNAPSHOT_TTL = 60
_snapshots: TTLCache = TTLCache(maxsize=1000, ttl=SNAPSHOT_TTL)
_snapshot_locks: dict[int, asyncio.Lock] = {}
_http = httpx.AsyncClient(
    headers={"User-Agent": settings.user_agent},
    timeout=httpx.Timeout(10.0, connect=5.0),
    follow_redirects=False,
)
MAX_SNAPSHOT_BYTES = 8_000_000


async def _download(url: str) -> tuple[str, bytes]:
    for _ in range(4):
        await assert_public_url(url)
        async with _http.stream("GET", url) as resp:
            if resp.is_redirect and resp.headers.get("location"):
                url = str(resp.url.join(resp.headers["location"]))
                continue
            if resp.status_code != 200:
                raise HTTPException(502, f"source returned HTTP {resp.status_code}")
            content_type = resp.headers.get("content-type", "").split(";")[0]
            if not content_type.startswith("image/"):
                raise HTTPException(502, "source did not return an image")
            body = b""
            async for chunk in resp.aiter_bytes():
                body += chunk
                if len(body) > MAX_SNAPSHOT_BYTES:
                    raise HTTPException(502, "image too large")
            return content_type, body
    raise HTTPException(502, "too many redirects")


async def _load_snapshot(webcam_id: int) -> tuple[str, bytes] | HTTPException:
    url = await get_pool().fetchval(
        """
        SELECT e.url FROM webcam_endpoints e JOIN webcams w ON w.id = e.webcam_id
        WHERE e.webcam_id = $1 AND e.type = 'image' AND w.status <> 'rejected'
        ORDER BY (e.id = w.preview_endpoint_id) IS TRUE DESC, e.is_working DESC NULLS LAST
        LIMIT 1
        """,
        webcam_id,
    )
    if url is None:
        return HTTPException(404, "no image for this webcam")
    try:
        return await _download(url)
    except HTTPException as exc:
        return exc
    except BlockedURL as exc:
        return HTTPException(502, str(exc))
    except httpx.HTTPError as exc:
        return HTTPException(502, f"source unreachable: {type(exc).__name__}")


@router.get("/{webcam_id}/snapshot", response_class=Response)
async def snapshot(webcam_id: int):
    cached = _snapshots.get(webcam_id)
    if cached is None:
        lock = _snapshot_locks.setdefault(webcam_id, asyncio.Lock())
        try:
            async with lock:
                cached = _snapshots.get(webcam_id)
                if cached is None:
                    # Failures are cached too: a dead source is not retried by every viewer.
                    cached = _snapshots[webcam_id] = await _load_snapshot(webcam_id)
        finally:
            if not lock.locked():
                _snapshot_locks.pop(webcam_id, None)
    if isinstance(cached, HTTPException):
        raise cached
    content_type, body = cached
    return Response(
        body,
        media_type=content_type,
        headers={"Cache-Control": f"public, max-age={SNAPSHOT_TTL}"},
    )
