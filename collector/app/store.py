"""Normalised records -> canonical webcams, with cross-source deduplication."""

from dataclasses import dataclass, field

import psycopg
from psycopg.types.json import Jsonb

# Two records from *different* sources are the same camera if they are closer than
# MERGE_TIGHT_M, or closer than MERGE_NAMED_M with similar names.
MERGE_TIGHT_M = 25
MERGE_NAMED_M = 150
NAME_SIMILARITY = 0.4

# Endpoint types we cannot health-check ourselves: trusted on insert.
TRUSTED_TYPES = {"youtube", "iframe"}

# Preview preference: a snapshot loads instantly everywhere; then live video; then players.
PREVIEW_ORDER = (
    "CASE e.type WHEN 'image' THEN 0 WHEN 'hls' THEN 1 WHEN 'mjpeg' THEN 2 WHEN 'dash' THEN 3 "
    "WHEN 'youtube' THEN 4 WHEN 'mp4' THEN 5 WHEN 'iframe' THEN 6 ELSE 7 END"
)


@dataclass
class Endpoint:
    type: str
    url: str
    width: int | None = None
    height: int | None = None


@dataclass
class WebcamRecord:
    source: str
    source_id: str
    latitude: float
    longitude: float
    name: str | None = None
    description: str | None = None
    country_code: str | None = None
    region: str | None = None
    city: str | None = None
    source_url: str | None = None
    webpage_url: str | None = None
    endpoints: list[Endpoint] = field(default_factory=list)
    raw: dict | None = None

    def params(self) -> dict:
        return {
            "source": self.source,
            "source_id": self.source_id,
            "lat": self.latitude,
            "lon": self.longitude,
            "name": self.name,
            "description": self.description,
            "country_code": (self.country_code or "").upper()[:2] or None,
            "region": self.region,
            "city": self.city,
            "source_url": self.source_url,
            "webpage_url": self.webpage_url,
            "raw": Jsonb(self.raw) if self.raw is not None else None,
        }


POINT = "ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326)"

FIND_BY_SOURCE = "SELECT webcam_id FROM webcam_sources WHERE source = %(source)s AND source_id = %(source_id)s"

# Only media URLs identify a camera: one page can list many cameras.
FIND_BY_URL = """
SELECT webcam_id FROM webcam_endpoints
WHERE md5(url) = md5(%(url)s) AND url = %(url)s AND type NOT IN ('page', 'iframe')
"""

FIND_NEARBY = f"""
SELECT w.id
FROM webcams w
WHERE ST_DWithin(w.geom::geography, {POINT}::geography, {MERGE_NAMED_M})
  AND w.status <> 'rejected'
  AND NOT EXISTS (
      SELECT 1 FROM webcam_sources s WHERE s.webcam_id = w.id AND s.source = %(source)s
  )
  AND (
      ST_DWithin(w.geom::geography, {POINT}::geography, {MERGE_TIGHT_M})
      OR (w.name IS NOT NULL AND %(name)s::text IS NOT NULL
          AND similarity(w.name, %(name)s::text) >= {NAME_SIMILARITY})
  )
ORDER BY w.geom::geography <-> {POINT}::geography
LIMIT 1
"""

INSERT_WEBCAM = f"""
INSERT INTO webcams (name, description, geom, country_code, region, city)
VALUES (%(name)s, %(description)s, {POINT}, %(country_code)s, %(region)s, %(city)s)
RETURNING id
"""

# The webcam's own fields follow its source when it has only one; once merged,
# sources only fill the blanks (first source wins, no flapping between sources).
UPDATE_WEBCAM = f"""
UPDATE webcams w SET
    name         = CASE WHEN s.sole THEN COALESCE(%(name)s, w.name) ELSE COALESCE(w.name, %(name)s) END,
    description  = CASE WHEN s.sole THEN COALESCE(%(description)s, w.description)
                        ELSE COALESCE(w.description, %(description)s) END,
    geom         = CASE WHEN s.sole AND w.submitted_by IS NULL THEN {POINT} ELSE w.geom END,
    country_code = COALESCE(w.country_code, %(country_code)s),
    region       = COALESCE(w.region, %(region)s),
    city         = COALESCE(w.city, %(city)s),
    last_seen    = now(),
    updated_at   = now()
FROM (SELECT count(*) <= 1 AS sole FROM webcam_sources WHERE webcam_id = %(webcam_id)s) s
WHERE w.id = %(webcam_id)s
"""

UPSERT_SOURCE = """
INSERT INTO webcam_sources (webcam_id, source, source_id, source_url, webpage_url, raw)
VALUES (%(webcam_id)s, %(source)s, %(source_id)s, %(source_url)s, %(webpage_url)s, %(raw)s)
ON CONFLICT (source, source_id) DO UPDATE SET
    source_url  = EXCLUDED.source_url,
    webpage_url = EXCLUDED.webpage_url,
    raw         = EXCLUDED.raw,
    last_seen   = now(),
    updated_at  = now()
"""

INSERT_ENDPOINT = """
INSERT INTO webcam_endpoints (webcam_id, type, url, origin, width, height, is_working)
VALUES (%s, %s, %s, %s, %s, %s, %s)
ON CONFLICT DO NOTHING
"""

REFRESH_LIVE = f"""
UPDATE webcams w SET
    preview_endpoint_id = p.endpoint_id,
    is_live = p.endpoint_id IS NOT NULL,
    updated_at = now()
FROM (
    SELECT wid, (
        SELECT e.id FROM webcam_endpoints e
        WHERE e.webcam_id = wid AND e.is_working
        ORDER BY {PREVIEW_ORDER}, e.id
        LIMIT 1
    ) AS endpoint_id
    FROM unnest(%s::bigint[]) AS wid
) p
WHERE w.id = p.wid
  AND (w.preview_endpoint_id IS DISTINCT FROM p.endpoint_id
       OR w.is_live IS DISTINCT FROM (p.endpoint_id IS NOT NULL))
"""


async def _one(conn: psycopg.AsyncConnection, sql: str, params) -> dict | None:
    cur = await conn.execute(sql, params)
    return await cur.fetchone()


async def upsert(conn: psycopg.AsyncConnection, rec: WebcamRecord, origin: str | None = None) -> str:
    """Returns 'inserted', 'updated' or 'duplicate' (attached to an existing webcam)."""
    p = rec.params()

    row = await _one(conn, FIND_BY_SOURCE, p)
    if row:
        outcome, webcam_id = "updated", row["webcam_id"]
    else:
        webcam_id = None
        for ep in rec.endpoints:
            if ep.type in ("page", "iframe"):
                continue
            row = await _one(conn, FIND_BY_URL, {"url": ep.url})
            if row:
                webcam_id = row["webcam_id"]
                break
        if webcam_id is None:
            row = await _one(conn, FIND_NEARBY, p)
            if row:
                webcam_id = row["id"]
        if webcam_id is None:
            outcome = "inserted"
            webcam_id = (await _one(conn, INSERT_WEBCAM, p))["id"]
        else:
            outcome = "duplicate"

    p["webcam_id"] = webcam_id
    if outcome != "inserted":
        await conn.execute(UPDATE_WEBCAM, p)
    await conn.execute(UPSERT_SOURCE, p)

    for ep in rec.endpoints:
        await conn.execute(
            INSERT_ENDPOINT,
            (
                webcam_id,
                ep.type,
                ep.url,
                origin or rec.source,
                ep.width,
                ep.height,
                True if ep.type in TRUSTED_TYPES else None,
            ),
        )
    if any(ep.type in TRUSTED_TYPES for ep in rec.endpoints):
        await refresh_live(conn, [webcam_id])
    return outcome


async def refresh_live(conn: psycopg.AsyncConnection, webcam_ids: list[int]) -> None:
    if webcam_ids:
        await conn.execute(REFRESH_LIVE, (list(set(webcam_ids)),))


async def delete_stale_sources(conn: psycopg.AsyncConnection, source: str, seen_since) -> int:
    """Forget links a (complete) run no longer reports, then webcams left without any source."""
    cur = await conn.execute(
        "DELETE FROM webcam_sources WHERE source = %s AND last_seen < %s", (source, seen_since)
    )
    removed = cur.rowcount
    await delete_orphans(conn)
    return removed


async def delete_orphans(conn: psycopg.AsyncConnection) -> None:
    """Webcams no source reports any more (user submissions are kept)."""
    await conn.execute(
        """
        DELETE FROM webcams w
        WHERE w.submitted_by IS NULL
          AND NOT EXISTS (SELECT 1 FROM webcam_sources s WHERE s.webcam_id = w.id)
        """
    )
