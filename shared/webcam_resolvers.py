"""Find the *current* image of providers that have no stable image URL.

Shared by the collector (health checks) and the API (snapshot proxy): both fetch the
provider page themselves and call `resolve()` on its HTML. Pure functions, no I/O.
"""

import re
from urllib.parse import urljoin

_OG_IMAGE = re.compile(
    r"""<meta[^>]+(?:property|name)=["'](?:og:image|twitter:image)["'][^>]+content=["']([^"']+)["']""",
    re.I,
)


def og_image(html: str, base_url: str) -> str | None:
    match = _OG_IMAGE.search(html)
    return urljoin(base_url, match.group(1)) if match else None


RESOLVERS = {
    # og:image is the latest capture, e.g. .../valmorel/planchamp/2026/09/28/large/23-00.jpg
    "skaping": og_image,
}


def resolve(resolver: str, html: str, base_url: str) -> str | None:
    fn = RESOLVERS.get(resolver)
    return fn(html, base_url) if fn else None
