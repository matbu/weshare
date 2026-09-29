"""Provider-specific extractors: turn a webcam *page* into real media endpoints.

Each extractor receives the already-fetched page and returns endpoints. Prefer stable URLs
that always serve the latest capture; when a provider has none, return the page URL with a
`resolver` (see shared/webcam_resolvers.py) so the current image is found at fetch time.
"""

import json
import logging
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from webcam_resolvers import og_image, resolve

from .polite import FetchError, Fetched, PoliteClient

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Found:
    type: str
    url: str
    resolver: str | None = None


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def _is(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def frame_allowed(headers) -> bool:
    """Can the page be shown in an <iframe> on another site?"""
    xfo = headers.get("x-frame-options", "").lower()
    if "deny" in xfo or "sameorigin" in xfo:
        return False
    csp = headers.get("content-security-policy", "").lower()
    match = re.search(r"frame-ancestors([^;]*)", csp)
    if match and "*" not in match.group(1).split():
        return False
    return True


# --- Roundshot: https://<site>.roundshot.com/ ------------------------------------------
# og:image is "/cams/<id>", which redirects to the latest panorama; "/cams/<id>/default"
# is the 2068x800 variant (~70 KB instead of ~550 KB for the full one).
ROUNDSHOT_CAM = re.compile(r"/cams/(\d+)")


async def roundshot(client: PoliteClient, page: Fetched, html: str) -> list[Found]:
    image = og_image(html, page.url)
    match = ROUNDSHOT_CAM.search(urlsplit(image).path) if image else None
    if not match:
        return []
    base = f"{urlsplit(page.url).scheme}://{_host(page.url)}"
    return [Found("image", f"{base}/cams/{match.group(1)}/default")]


# --- webcam-hd / Trinum: https://app.webcam-hd.com/<group>/<camera> ---------------------
# The Angular app reads smr/json/webcam_display_group/<group>.json; each camera has a
# "webcam_display_str_image" id whose latest image is www.trinum.com/ibox/ftpcam/<id>.jpg.
TRINUM_IMAGE = "https://www.trinum.com/ibox/ftpcam/{}.jpg"


async def webcam_hd(client: PoliteClient, page: Fetched, html: str) -> list[Found]:
    parts = [p for p in urlsplit(page.url).path.split("/") if p]
    if not parts:
        return []
    group, camera = parts[0], (parts[1] if len(parts) > 1 else None)
    base = f"{urlsplit(page.url).scheme}://{_host(page.url)}"
    try:
        resp = await client.fetch(f"{base}/smr/json/webcam_display_group/{group}.json", max_bytes=1_000_000)
        cams = json.loads(resp.body) if resp.status == 200 else []
    except (FetchError, ValueError) as exc:
        log.debug("webcam-hd %s: %s", page.url, exc)
        return []
    if not isinstance(cams, list):
        return []
    cams = sorted(
        (c for c in cams if isinstance(c, dict) and c.get("webcam_display_str_image")),
        key=lambda c: c.get("webcam_display_in_group_web_page_tri") or 0,
    )
    chosen = next((c for c in cams if c.get("url_part_2") == camera), None) if camera else None
    chosen = chosen or (cams[0] if cams else None)
    if not chosen:
        return []
    return [Found("image", TRINUM_IMAGE.format(chosen["webcam_display_str_image"]))]


# --- Skaping: https://www.skaping.com/<site>/<camera> -----------------------------------
# No stable URL: the latest image is only in the page (og:image), resolved at fetch time.
async def skaping(client: PoliteClient, page: Fetched, html: str) -> list[Found]:
    if len([p for p in urlsplit(page.url).path.split("/") if p]) < 2:
        return []
    if not resolve("skaping", html, page.url):
        return []
    return [Found("image", page.url, resolver="skaping")]


EXTRACTORS = (
    ("roundshot.com", roundshot),
    ("webcam-hd.com", webcam_hd),
    ("skaping.com", skaping),
)


async def extract(client: PoliteClient, page: Fetched, html: str) -> list[Found] | None:
    """Endpoints found by a dedicated extractor, or None when no extractor handles this host."""
    host = _host(page.url)
    for domain, extractor in EXTRACTORS:
        if _is(host, domain):
            return await extractor(client, page, html)
    return None
