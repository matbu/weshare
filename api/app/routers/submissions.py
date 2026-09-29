"""Webcams added by users. They start 'pending' until a moderator approves them;
the collector health-checks their media URL in the meantime."""

from fastapi import APIRouter, Depends, HTTPException
from webcam_policy import is_blocked

from .. import snapshots

from ..config import settings
from ..db import get_pool
from ..media import BlockedURL, assert_public_url, classify_url
from ..schemas import Moderation, WebcamSubmission, WebcamSummary
from ..security import current_user, moderator
from .webcams import PREVIEW_JOIN, SUMMARY

router = APIRouter(tags=["submissions"])


@router.post("/webcams", response_model=WebcamSummary, status_code=201)
async def submit_webcam(body: WebcamSubmission, user: dict = Depends(current_user)):
    urls = [str(u) for u in (body.media_url, body.page_url) if u]
    blocked = await snapshots.blocked_hosts()
    if any(is_blocked(u, blocked) for u in urls):
        raise HTTPException(422, "the owner of this site asked us not to show its webcams")
    for url in urls:
        try:
            await assert_public_url(url)
        except BlockedURL as exc:
            raise HTTPException(422, str(exc))

    endpoints = []
    if body.media_url:
        kind = classify_url(str(body.media_url))
        endpoints.append((kind, str(body.media_url)))
    if body.page_url:
        endpoints.append(("page", str(body.page_url)))

    is_mod = user["role"] in ("moderator", "admin")
    pool = get_pool()
    async with pool.acquire() as conn, conn.transaction():
        existing = await conn.fetchval(
            "SELECT webcam_id FROM webcam_endpoints WHERE type NOT IN ('page', 'iframe') "
            "AND md5(url) = ANY(SELECT md5(u) FROM unnest($1::text[]) u)",
            [url for kind, url in endpoints if kind != "page"],
        )
        if existing:
            raise HTTPException(409, {"message": "webcam already known", "webcam_id": existing})

        pending = await conn.fetchval(
            "SELECT count(*) FROM webcams WHERE submitted_by = $1 AND status = 'pending'", user["id"]
        )
        if not is_mod and pending >= settings.max_pending_per_user:
            raise HTTPException(429, "too many webcams waiting for moderation")

        webcam_id = await conn.fetchval(
            """
            INSERT INTO webcams (name, description, geom, status, submitted_by)
            VALUES ($1, $2, ST_SetSRID(ST_MakePoint($4, $3), 4326), $5::webcam_status, $6)
            RETURNING id
            """,
            body.name, body.description, body.latitude, body.longitude,
            "approved" if is_mod else "pending", user["id"],
        )
        await conn.execute(
            "INSERT INTO webcam_sources (webcam_id, source, source_id, webpage_url) VALUES ($1, 'user', $2, $3)",
            webcam_id, str(webcam_id), str(body.page_url) if body.page_url else None,
        )
        await conn.executemany(
            "INSERT INTO webcam_endpoints (webcam_id, type, url, origin, is_working) "
            "VALUES ($1, $2::endpoint_type, $3, 'user', $4)",
            [(webcam_id, kind, url, True if kind in ("youtube", "iframe") else None) for kind, url in endpoints],
        )
        row = await conn.fetchrow(f"SELECT {SUMMARY} FROM webcams w {PREVIEW_JOIN} WHERE w.id = $1", webcam_id)
    return WebcamSummary.from_row(row)


@router.get("/me/webcams", response_model=list[WebcamSummary])
async def my_webcams(user: dict = Depends(current_user)):
    rows = await get_pool().fetch(
        f"SELECT {SUMMARY} FROM webcams w {PREVIEW_JOIN} WHERE w.submitted_by = $1 ORDER BY w.id DESC",
        user["id"],
    )
    return [WebcamSummary.from_row(r) for r in rows]


@router.get("/admin/pending", response_model=list[WebcamSummary], tags=["admin"])
async def pending(limit: int = 100, _: dict = Depends(moderator)):
    rows = await get_pool().fetch(
        f"SELECT {SUMMARY} FROM webcams w {PREVIEW_JOIN} WHERE w.status = 'pending' "
        f"ORDER BY w.created_at LIMIT $1",
        min(limit, 500),
    )
    return [WebcamSummary.from_row(r) for r in rows]


@router.post("/admin/webcams/{webcam_id}/moderate", tags=["admin"])
async def moderate(webcam_id: int, body: Moderation, _: dict = Depends(moderator)):
    status = "approved" if body.action == "approve" else "rejected"
    updated = await get_pool().fetchval(
        "UPDATE webcams SET status = $1::webcam_status, updated_at = now() WHERE id = $2 RETURNING id",
        status, webcam_id,
    )
    if updated is None:
        raise HTTPException(404, "webcam not found")
    return {"id": webcam_id, "status": status}


@router.delete("/admin/webcams/{webcam_id}", status_code=204, tags=["admin"])
async def delete_webcam(webcam_id: int, _: dict = Depends(moderator)):
    await get_pool().execute("DELETE FROM webcams WHERE id = $1", webcam_id)
