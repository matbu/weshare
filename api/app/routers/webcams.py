import email.utils
import random

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response

from .. import snapshots
from ..db import get_pool
from ..schemas import WebcamDetail, WebcamSummary
from ..security import optional_user

router = APIRouter(prefix="/webcams", tags=["webcams"])

SUMMARY = """
    w.id, w.name, w.latitude, w.longitude, w.country_code, w.region, w.city,
    w.is_live, w.status::text AS status,
    pe.type::text AS preview_type, pe.url AS preview_url, pe.live AS preview_live,
    pe.resolver AS preview_resolver,
    (SELECT e.url FROM webcam_endpoints e
     WHERE e.webcam_id = w.id AND e.type IN ('iframe', 'youtube') AND e.is_working
       AND NOT EXISTS (SELECT 1 FROM sources x WHERE x.name = e.origin AND NOT x.enabled)
     ORDER BY e.type = 'youtube' DESC, e.id LIMIT 1) AS embed_url,
    (SELECT e.url FROM webcam_endpoints e
     WHERE e.webcam_id = w.id AND e.type = 'page' ORDER BY e.id LIMIT 1) AS page_url
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
        WHERE w.is_live AND w.status = 'approved' AND w.source_enabled
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
        WHERE w.status = 'approved' AND w.source_enabled AND (NOT $5 OR w.is_live)
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
        WHERE w.status = 'approved' AND w.source_enabled AND (NOT $6 OR w.is_live)
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
        SELECT {SUMMARY}, w.description, w.first_seen, w.submitted_by, w.source_enabled
        FROM webcams w {PREVIEW_JOIN} WHERE w.id = $1
        """,
        webcam_id,
    )
    if row is None:
        raise HTTPException(404, "webcam not found")
    mine = user is not None and row["submitted_by"] == user["id"]
    is_mod = user is not None and user["role"] in ("moderator", "admin")
    if (row["status"] != "approved" or not row["source_enabled"]) and not (mine or is_mod):
        raise HTTPException(404, "webcam not found")

    endpoints = await pool.fetch(
        "SELECT id, type::text AS type, url, origin, is_working, live, width, height, last_success "
        "FROM webcam_endpoints e WHERE webcam_id = $1 AND NOT EXISTS (SELECT 1 FROM sources x WHERE x.name = e.origin AND NOT x.enabled) "
        "ORDER BY is_working DESC NULLS LAST, id",
        webcam_id,
    )
    sources = await pool.fetch(
        "SELECT s.source, s.source_id, s.source_url, s.webpage_url FROM webcam_sources s "
        "JOIN sources x ON x.name = s.source AND x.enabled WHERE s.webcam_id = $1 ORDER BY s.id",
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


@router.get("/{webcam_id}/snapshot", response_class=Response)
async def snapshot(webcam_id: int, if_none_match: str | None = Header(None)):
    """Latest image of the webcam (source polled every 3 to 60 s depending on how often it changes)."""
    snap = await snapshots.get(webcam_id)
    etag = f'"{snap.digest}"'
    headers = {
        "ETag": etag,
        "Last-Modified": email.utils.format_datetime(snap.updated_at, usegmt=True),
        "X-Image-Updated": snap.updated_at.isoformat(),
        "Cache-Control": f"public, max-age={int(snapshots.MIN_INTERVAL) - 1}",
    }
    if if_none_match == etag:
        return Response(status_code=304, headers=headers)
    return Response(snap.body, media_type=snap.content_type, headers=headers)
