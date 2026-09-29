"""Owners asking us to stop showing their webcams, and the moderation of those requests.

- scope 'webcam': the webcam is hidden immediately (status -> pending) and waits for a
  moderator: 'remove' rejects it for good (re-imports keep it hidden), 'dismiss' restores it.
- scope 'site': reviewed first; 'block_site' blocks the host: no more collection, embedding
  or proxying, and every webcam served from it is rejected.
"""

import time
from collections import defaultdict, deque
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, EmailStr, Field
from webcam_policy import host_of

from ..db import get_pool
from ..security import moderator

router = APIRouter(tags=["removals"])

MAX_REQUESTS_PER_HOUR = 5
_recent: dict[str, deque] = defaultdict(deque)


class RemovalIn(BaseModel):
    scope: Literal["webcam", "site"]
    webcam_id: int | None = None
    url: str | None = Field(default=None, max_length=2000, description="Webcam or site URL")
    email: EmailStr
    requester: str | None = Field(default=None, max_length=200, description="Name / organisation")
    message: str | None = Field(default=None, max_length=4000)
    website: str | None = Field(default=None, description="Honeypot: must stay empty")


class Processing(BaseModel):
    action: Literal["remove", "block_site", "dismiss"]


def _rate_limited(ip: str) -> bool:
    now, window = time.monotonic(), _recent[ip]
    while window and now - window[0] > 3600:
        window.popleft()
    if len(window) >= MAX_REQUESTS_PER_HOUR:
        return True
    window.append(now)
    return False


# Every host a webcam is shown from: its endpoints (media, player, page) and source pages.
WEBCAM_URLS = """
SELECT url FROM webcam_endpoints WHERE webcam_id = $1
UNION SELECT webpage_url FROM webcam_sources WHERE webcam_id = $1 AND webpage_url IS NOT NULL
"""


@router.post("/removal-requests", status_code=202)
async def request_removal(body: RemovalIn, request: Request):
    if body.website:  # bots fill every field
        return {"status": "received"}
    if body.scope == "webcam" and not body.webcam_id and not body.url:
        raise HTTPException(422, "webcam_id or url is required")
    if body.scope == "site" and not (body.url and host_of(body.url)):
        raise HTTPException(422, "the site URL is required")
    if _rate_limited(request.client.host if request.client else "?"):
        raise HTTPException(429, "too many requests, please write to us by e-mail")

    pool = get_pool()
    async with pool.acquire() as conn, conn.transaction():
        webcam_id = body.webcam_id
        if webcam_id is None and body.url and body.scope == "webcam":
            webcam_id = await conn.fetchval(
                "SELECT webcam_id FROM webcam_endpoints WHERE url = $1 LIMIT 1", body.url
            )
        if webcam_id is not None and not await conn.fetchval("SELECT 1 FROM webcams WHERE id = $1", webcam_id):
            webcam_id = None
        request_id = await conn.fetchval(
            """
            INSERT INTO removal_requests (webcam_id, url, scope, email, requester, message)
            VALUES ($1, $2, $3, $4, $5, $6) RETURNING id
            """,
            webcam_id, body.url, body.scope, body.email.lower(), body.requester, body.message,
        )
        hidden = False
        if body.scope == "webcam" and webcam_id is not None:
            # Hidden right away, until a moderator confirms or restores it.
            hidden = await conn.fetchval(
                "UPDATE webcams SET status = 'pending', updated_at = now() "
                "WHERE id = $1 AND status = 'approved' RETURNING true",
                webcam_id,
            ) or False
    return {"status": "received", "id": request_id, "hidden": hidden}


@router.get("/admin/removal-requests", tags=["admin"])
async def list_requests(status: str = "pending", _: dict = Depends(moderator)):
    rows = await get_pool().fetch(
        """
        SELECT r.id, r.webcam_id, r.url, r.scope, r.email, r.requester, r.message, r.status,
               r.created_at, w.name AS webcam_name
        FROM removal_requests r LEFT JOIN webcams w ON w.id = r.webcam_id
        WHERE r.status = $1 ORDER BY r.created_at LIMIT 200
        """,
        status,
    )
    return [dict(r) for r in rows]


async def _reject_hosts(conn, hosts: set[str]) -> int:
    """Reject every webcam shown from one of these hosts, and drop those endpoints."""
    patterns = [h for h in hosts] + [f"%.{h}" for h in hosts]
    webcam_ids = await conn.fetch(
        """
        SELECT DISTINCT webcam_id FROM (
            SELECT webcam_id, url FROM webcam_endpoints
            UNION ALL SELECT webcam_id, webpage_url FROM webcam_sources WHERE webpage_url IS NOT NULL
        ) u
        WHERE regexp_replace(lower(split_part(split_part(url, '://', 2), '/', 1)), '^www\\.|:\\d+$', '', 'g')
              LIKE ANY($1::text[])
        """,
        patterns,
    )
    ids = [r["webcam_id"] for r in webcam_ids]
    await conn.execute(
        "UPDATE webcams SET status = 'rejected', updated_at = now() WHERE id = ANY($1::bigint[])", ids
    )
    return len(ids)


@router.post("/admin/removal-requests/{request_id}/process", tags=["admin"])
async def process(request_id: int, body: Processing, user: dict = Depends(moderator)):
    pool = get_pool()
    async with pool.acquire() as conn, conn.transaction():
        req = await conn.fetchrow("SELECT * FROM removal_requests WHERE id = $1 FOR UPDATE", request_id)
        if req is None:
            raise HTTPException(404, "request not found")
        result: dict = {"id": request_id, "action": body.action}

        if body.action == "remove":
            if req["webcam_id"] is None:
                raise HTTPException(422, "this request has no webcam: use block_site or dismiss")
            await conn.execute(
                "UPDATE webcams SET status = 'rejected', updated_at = now() WHERE id = $1", req["webcam_id"]
            )
        elif body.action == "block_site":
            hosts = {host_of(req["url"])} if req["url"] else set()
            if req["webcam_id"] is not None and req["scope"] == "webcam" and not hosts:
                hosts = {host_of(r["url"]) for r in await conn.fetch(WEBCAM_URLS, req["webcam_id"])}
            hosts.discard("")
            if not hosts:
                raise HTTPException(422, "no site to block in this request")
            for host in hosts:
                await conn.execute(
                    "INSERT INTO blocked_hosts (host, reason, request_id) VALUES ($1, $2, $3) "
                    "ON CONFLICT (host) DO NOTHING",
                    host, f"removal request #{request_id}", request_id,
                )
            result["blocked_hosts"] = sorted(hosts)
            result["webcams_rejected"] = await _reject_hosts(conn, hosts)
        else:  # dismiss: restore a webcam hidden by this request
            if req["scope"] == "webcam" and req["webcam_id"] is not None:
                await conn.execute(
                    "UPDATE webcams SET status = 'approved', updated_at = now() "
                    "WHERE id = $1 AND status = 'pending' AND submitted_by IS NULL",
                    req["webcam_id"],
                )

        await conn.execute(
            "UPDATE removal_requests SET status = $1, handled_at = now(), handled_by = $2 WHERE id = $3",
            "dismissed" if body.action == "dismiss" else "done", user["id"], request_id,
        )
    return result


@router.get("/admin/blocked-hosts", tags=["admin"])
async def list_blocked(_: dict = Depends(moderator)):
    rows = await get_pool().fetch("SELECT host, reason, request_id, created_at FROM blocked_hosts ORDER BY host")
    return [dict(r) for r in rows]


@router.post("/admin/blocked-hosts", status_code=201, tags=["admin"])
async def block_host(url: str, reason: str | None = None, _: dict = Depends(moderator)):
    """Block a site directly (without a removal request)."""
    host = host_of(url if "://" in url else f"https://{url}")
    if not host:
        raise HTTPException(422, "invalid host")
    async with get_pool().acquire() as conn, conn.transaction():
        await conn.execute(
            "INSERT INTO blocked_hosts (host, reason) VALUES ($1, $2) ON CONFLICT (host) DO NOTHING", host, reason
        )
        rejected = await _reject_hosts(conn, {host})
    return {"host": host, "webcams_rejected": rejected}


@router.delete("/admin/blocked-hosts/{host}", status_code=204, tags=["admin"])
async def unblock_host(host: str, _: dict = Depends(moderator)):
    """Unblock a host. Its webcams stay rejected: restore them one by one if needed."""
    await get_pool().execute("DELETE FROM blocked_hosts WHERE host = $1", host.lower())
