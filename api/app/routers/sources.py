"""Switch data sources on/off (e.g. hide every Windy webcam) without deleting anything."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..db import get_pool
from ..security import moderator
from . import tiles

router = APIRouter(tags=["sources"])


class SourceToggle(BaseModel):
    enabled: bool


SOURCES_SQL = """
SELECT x.name, x.label, x.enabled, x.updated_at,
       (SELECT count(*) FROM webcam_sources s WHERE s.source = x.name) AS webcams,
       (SELECT max(started_at) FROM source_runs r WHERE r.source = x.name) AS last_run
FROM sources x ORDER BY x.name
"""


@router.get("/admin/sources", tags=["admin"])
async def list_sources(_: dict = Depends(moderator)):
    return [dict(r) for r in await get_pool().fetch(SOURCES_SQL)]


@router.put("/admin/sources/{name}", tags=["admin"])
async def toggle_source(name: str, body: SourceToggle, _: dict = Depends(moderator)):
    pool = get_pool()
    async with pool.acquire() as conn, conn.transaction():
        updated = await conn.fetchval(
            "UPDATE sources SET enabled = $1, updated_at = now() WHERE name = $2 RETURNING name",
            body.enabled, name,
        )
        if updated is None:
            raise HTTPException(404, "unknown source")
        # Every webcam this source reports or provides an endpoint for: visibility + preview.
        ids = await conn.fetchval(
            """
            SELECT array_agg(webcam_id) FROM (
                SELECT webcam_id FROM webcam_sources WHERE source = $1
                UNION SELECT webcam_id FROM webcam_endpoints WHERE origin = $1
            ) u
            """,
            name,
        ) or []
        await conn.execute("SELECT refresh_webcams($1::bigint[])", ids)
    tiles._cache.clear()  # low-zoom map tiles are cached in memory
    return {"name": name, "enabled": body.enabled, "webcams_affected": len(ids)}
