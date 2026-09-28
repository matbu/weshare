"""Offline reverse geocoding (GeoNames cities, in memory).

We never call Nominatim per webcam: its usage policy forbids bulk geocoding.
"""

import asyncio
import logging

from .store import WebcamRecord

log = logging.getLogger(__name__)

_rg = None


def _load():
    global _rg
    if _rg is None:
        import reverse_geocoder

        _rg = reverse_geocoder
    return _rg


def lookup(coords: list[tuple[float, float]]) -> list[dict]:
    if not coords:
        return []
    return _load().search(coords, mode=1, verbose=False)


async def enrich(records: list[WebcamRecord]) -> None:
    todo = [r for r in records if not (r.country_code and r.city)]
    if not todo:
        return
    try:
        results = await asyncio.to_thread(lookup, [(r.latitude, r.longitude) for r in todo])
    except Exception:
        log.exception("reverse geocoding failed")
        return
    for rec, res in zip(todo, results):
        rec.country_code = rec.country_code or res.get("cc") or None
        rec.region = rec.region or res.get("admin1") or None
        rec.city = rec.city or res.get("name") or None


async def backfill(conn, limit: int = 5000) -> int:
    """Fill country/region/city for webcams created without them (e.g. user submissions)."""
    cur = await conn.execute(
        "SELECT id, latitude, longitude FROM webcams WHERE country_code IS NULL "
        "ORDER BY id LIMIT %s",
        (limit,),
    )
    rows = await cur.fetchall()
    if not rows:
        return 0
    results = await asyncio.to_thread(lookup, [(r["latitude"], r["longitude"]) for r in rows])
    async with conn.cursor() as cur:
        await cur.executemany(
            "UPDATE webcams SET country_code = %s, region = COALESCE(region, %s), "
            "city = COALESCE(city, %s) WHERE id = %s",
            [
                (res.get("cc") or None, res.get("admin1") or None, res.get("name") or None, row["id"])
                for row, res in zip(rows, results)
            ],
        )
    return len(rows)
