import json

from fastapi import APIRouter

from ..db import get_pool

router = APIRouter(tags=["stats"])


@router.get("/stats")
async def stats():
    pool = get_pool()
    totals = await pool.fetchrow(
        """
        SELECT count(*) FILTER (WHERE status = 'approved') AS webcams,
               count(*) FILTER (WHERE status = 'approved' AND is_live) AS live,
               count(*) FILTER (WHERE status = 'pending') AS pending,
               count(DISTINCT country_code) AS countries
        FROM webcams
        """
    )
    sources = await pool.fetch(
        "SELECT source, count(*) AS webcams FROM webcam_sources GROUP BY source ORDER BY 2 DESC"
    )
    runs = await pool.fetch(
        """
        SELECT DISTINCT ON (source) source, started_at, finished_at, status,
               discovered, inserted, updated, duplicates, errors, stats
        FROM source_runs
        ORDER BY source, started_at DESC
        """
    )
    return {
        **dict(totals),
        "sources": [dict(r) for r in sources],
        "last_runs": [dict(r) | {"stats": json.loads(r["stats"])} for r in runs],
    }
