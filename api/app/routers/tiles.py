"""Mapbox Vector Tiles straight from PostGIS (MapLibre on web, iOS and Android).

Below CLUSTER_MAX_ZOOM webcams are aggregated server-side on a grid, so a single tile
never ships more than a few thousand features even with millions of webcams.
Layer "webcams", properties: id (single webcam only), count, live, name.
"""

import math

from cachetools import TTLCache
from fastapi import APIRouter, HTTPException, Response

from ..db import get_pool

router = APIRouter(tags=["map"])

CLUSTER_MAX_ZOOM = 9
MAX_ZOOM = 16
EXTENT = 4096
WORLD_M = 40_075_016.685_578_5
GRID_PER_TILE = 48  # clusters are ~ tile_size / 48 apart

# Low zoom tiles are few and expensive: keep them in memory.
_cache: TTLCache = TTLCache(maxsize=4096, ttl=600)

CLUSTERS_SQL = f"""
WITH b AS (
    SELECT ST_TileEnvelope($1, $2, $3) AS env, ST_Transform(ST_TileEnvelope($1, $2, $3), 4326) AS env4326
),
pts AS (
    SELECT w.id, w.name, w.is_live, ST_Transform(w.geom, 3857) AS g
    FROM webcams w, b
    WHERE w.status = 'approved' AND w.source_enabled AND w.geom && b.env4326
),
clusters AS (
    SELECT ST_Centroid(ST_Collect(g)) AS g,
           count(*) AS count,
           count(*) FILTER (WHERE is_live) AS live,
           CASE WHEN count(*) = 1 THEN min(id) END AS id,
           CASE WHEN count(*) = 1 THEN min(name) END AS name
    FROM pts
    GROUP BY ST_SnapToGrid(g, $4)
)
SELECT ST_AsMVT(t, 'webcams', {EXTENT}, 'geom') FROM (
    SELECT c.id, c.name, c.count::int AS count, c.live::int AS live,
           ST_AsMVTGeom(c.g, b.env, {EXTENT}, 64, true) AS geom
    FROM clusters c, b
) t
"""

POINTS_SQL = f"""
WITH b AS (
    SELECT ST_TileEnvelope($1, $2, $3) AS env, ST_Transform(ST_TileEnvelope($1, $2, $3), 4326) AS env4326
)
SELECT ST_AsMVT(t, 'webcams', {EXTENT}, 'geom') FROM (
    SELECT w.id, w.name, 1 AS count, w.is_live::int AS live,
           ST_AsMVTGeom(ST_Transform(w.geom, 3857), b.env, {EXTENT}, 64, true) AS geom
    FROM webcams w, b
    WHERE w.status = 'approved' AND w.source_enabled AND w.geom && b.env4326
    LIMIT 20000
) t
"""


@router.get("/tiles/{z}/{x}/{y}.pbf", response_class=Response)
async def tile(z: int, x: int, y: int):
    if not (0 <= z <= MAX_ZOOM and 0 <= x < 2**z and 0 <= y < 2**z):
        raise HTTPException(404, "tile out of range")

    key = (z, x, y)
    data = _cache.get(key)
    if data is None:
        pool = get_pool()
        if z < CLUSTER_MAX_ZOOM:
            cell = WORLD_M / math.pow(2, z) / GRID_PER_TILE
            data = await pool.fetchval(CLUSTERS_SQL, z, x, y, cell)
        else:
            data = await pool.fetchval(POINTS_SQL, z, x, y)
        data = data or b""
        if z < CLUSTER_MAX_ZOOM:
            _cache[key] = data

    return Response(
        data,
        media_type="application/vnd.mapbox-vector-tile",
        headers={"Cache-Control": "public, max-age=300"},
    )
