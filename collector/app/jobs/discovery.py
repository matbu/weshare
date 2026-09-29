"""Looks inside webcam *pages* (e.g. OSM contact:webcam) for something to show.

Most OSM webcams only link to an HTML page. We fetch that page (robots.txt respected) and:
- known providers (Roundshot, webcam-hd, Skaping...): dedicated extractor (app.providers);
- other sites: generic extraction of likely snapshot / stream URLs;
- in both cases, the page itself as an embeddable player when it allows <iframe>.
Media found are validated by the health checker. Pages are re-examined once a month.
"""

import asyncio
import logging
import re
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import psycopg

from .. import db, geo, providers, store
from ..config import settings
from ..media import classify_url, mime_to_type, normalize_url
from ..probe import looks_like_stream, sniff
from ..polite import BlockedURL, FetchError, PoliteClient
from ..runs import RunTracker

log = logging.getLogger(__name__)

MAX_CANDIDATES = 3
KEYWORDS = re.compile(r"(webcam|[/_.-]cam\d*[/_.-]|snapshot|current|latest|live|image\.jpe?g|\.cgi)", re.I)
EXCLUDE = re.compile(r"(logo|icon|banner|sprite|avatar|favicon|button|\.svg)", re.I)
M3U8 = re.compile(r"""https?://[^\s"'<>\\]+?\.m3u8[^\s"'<>\\]*""", re.I)

SELECT_PAGES = """
SELECT id, webcam_id, url FROM webcam_endpoints
WHERE type = 'page' AND (discovered_at IS NULL OR discovered_at < now() - interval '30 days')
ORDER BY discovered_at NULLS FIRST, id
LIMIT %s
"""


class _Collector(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.images: list[str] = []
        self.og_images: list[str] = []
        self.media: list[tuple[str, str | None]] = []  # (src, type attribute)
        self.iframes: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = {k: v for k, v in attrs if v}
        if tag == "meta" and a.get("property", a.get("name", "")).lower() in ("og:image", "twitter:image"):
            self.og_images.append(a.get("content", ""))
        elif tag == "img":
            for key in ("src", "data-src", "data-original"):
                if key in a:
                    self.images.append(a[key])
        elif tag in ("video", "source"):
            if "src" in a:
                self.media.append((a["src"], a.get("type")))
        elif tag == "iframe" and "src" in a:
            self.iframes.append(a["src"])


def extract_candidates(html: str, base_url: str) -> list[tuple[str, str]]:
    """Returns [(type, absolute_url)] ordered from most to least promising."""
    parser = _Collector()
    try:
        parser.feed(html)
    except Exception:  # malformed markup: keep what was parsed
        pass

    found: list[tuple[str, str]] = []

    def add(raw: str, forced_type: str | None = None, need_keyword: bool = False):
        url = normalize_url(urljoin(base_url, raw.strip()))
        if not url or EXCLUDE.search(url) or (need_keyword and not KEYWORDS.search(url)):
            return
        kind = forced_type or classify_url(url)
        if kind == "page":
            return
        if all(u != url for _, u in found):
            found.append((kind, url))

    for url in M3U8.findall(html):
        add(url, "hls")
    for src, mime in parser.media:
        kind = mime_to_type(mime)
        if kind is None:
            guessed = classify_url(normalize_url(urljoin(base_url, src)) or "")
            # A <video> src without a known extension is still a video: the health
            # checker probes it and fixes the type if needed.
            kind = guessed if guessed != "page" else "mp4"
        add(src, kind)
    for src in parser.iframes:
        if classify_url(normalize_url(urljoin(base_url, src)) or "") == "youtube":
            add(src, "youtube")
    for src in parser.images:
        add(src, need_keyword=True)
    for src in parser.og_images:
        add(src, "image", need_keyword=True)
    return found[:MAX_CANDIDATES]


INSERT_FOUND = """
INSERT INTO webcam_endpoints (webcam_id, type, url, origin, resolver, is_working, next_check_at)
VALUES (%s, %s, %s, %s, %s, %s, now())
ON CONFLICT DO NOTHING
"""


async def _add(conn, page: dict, found: providers.Found, origin: str, tracker: RunTracker) -> None:
    trusted = found.type in ("youtube", "iframe")
    cur = await conn.execute(
        INSERT_FOUND,
        (page["webcam_id"], found.type, found.url, origin, found.resolver, True if trusted else None),
    )
    tracker.incr(f"found_{found.type}", cur.rowcount)
    tracker.incr("inserted", cur.rowcount)


async def _page_gone(conn, page: dict, tracker: RunTracker) -> None:
    """The page no longer answers: its embed is not worth showing either."""
    tracker.incr("unreachable")
    await conn.execute(
        "UPDATE webcam_endpoints SET is_working = false WHERE webcam_id = %s AND type = 'iframe' "
        "AND origin = 'discovery' AND url = %s",
        (page["webcam_id"], page["url"]),
    )


async def _discover(client: PoliteClient, conn, page: dict, tracker: RunTracker) -> None:
    url = page["url"]
    try:
        if not await client.allowed_by_robots(url):
            tracker.incr("robots_denied")
            return
        resp = await client.fetch(url, max_bytes=2_000_000, retries=1, until=looks_like_stream)
    except BlockedURL:
        tracker.incr("blocked")
        return
    except FetchError as exc:
        log.debug("discovery: %s: %s", url, exc)
        await _page_gone(conn, page, tracker)
        return

    if resp.status != 200:
        await _page_gone(conn, page, tracker)
        return

    # The "page" was in fact the media itself: decided from what it serves.
    direct = sniff(resp.content_type, resp.body)
    if direct == "ts":
        direct = None
    if direct:
        try:  # autocommit connection shared by concurrent tasks: no explicit transaction
            await conn.execute(
                "UPDATE webcam_endpoints SET type = %s, next_check_at = now() WHERE id = %s",
                (direct, page["id"]),
            )
            tracker.incr("inserted")
        except psycopg.errors.UniqueViolation:
            tracker.incr("duplicate_media")
        return

    if "html" not in resp.content_type:
        return
    charset = re.search(r"charset=([\w-]+)", resp.headers.get("content-type", ""))
    try:
        html = resp.body.decode(charset.group(1) if charset else "utf-8", "replace")
    except LookupError:
        html = resp.body.decode("utf-8", "replace")

    found = await providers.extract(client, resp, html)
    origin = "provider"
    if found is None:
        origin = "discovery"
        found = [providers.Found(kind, media_url) for kind, media_url in extract_candidates(html, resp.url)]
    for item in found:
        await _add(conn, page, item, origin, tracker)

    # Whatever we found, the provider's own player is the richest view (360°, timelapse,
    # live video): offer it when the site allows embedding.
    if providers.frame_allowed(resp.headers):
        await _add(conn, page, providers.Found("iframe", resp.url), "discovery", tracker)
    else:
        tracker.incr("frame_denied")


async def run(client: PoliteClient, tracker: RunTracker) -> None:
    async with await db.connect(autocommit=True) as conn:
        cur = await conn.execute(SELECT_PAGES, (settings.discovery_batch,))
        pages = await cur.fetchall()
        tracker.incr("discovered", len(pages))
        if pages:
            # Claim the batch first so a crash never makes us hammer the same pages.
            await conn.execute(
                "UPDATE webcam_endpoints SET discovered_at = now() WHERE id = ANY(%s)",
                ([p["id"] for p in pages],),
            )
        await asyncio.gather(*(_discover(client, conn, p, tracker) for p in pages))

        await store.refresh_live(conn, [p["webcam_id"] for p in pages])
        tracker.incr("geocoded", await geo.backfill(conn))
