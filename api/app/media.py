"""URL helpers shared by submissions and the snapshot proxy."""

import asyncio
import ipaddress
import re
import socket
from urllib.parse import urlsplit

IMAGE_EXT = (".jpg", ".jpeg", ".png", ".webp", ".gif")
IMAGE_PATTERNS = re.compile(r"(snapshot\.cgi|image\.cgi|/jpg/image|axis-cgi/jpg)", re.I)
MJPEG_PATTERNS = re.compile(r"(mjpe?g|axis-cgi/mjpg|video\.cgi|faststream)", re.I)
YOUTUBE_HOSTS = ("youtube.com", "youtu.be", "youtube-nocookie.com")


def classify_url(url: str) -> str:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    path = parts.path.lower()
    full = path + ("?" + parts.query.lower() if parts.query else "")
    if any(host == h or host.endswith("." + h) for h in YOUTUBE_HOSTS):
        return "youtube"
    if path.endswith(".m3u8"):
        return "hls"
    if path.endswith(".mpd"):
        return "dash"
    if path.endswith((".mp4", ".m4v", ".mov", ".webm")):
        return "mp4"
    if MJPEG_PATTERNS.search(full):
        return "mjpeg"
    if path.endswith(IMAGE_EXT) or IMAGE_PATTERNS.search(full):
        return "image"
    return "page"


class BlockedURL(Exception):
    pass


async def assert_public_url(url: str) -> None:
    """SSRF guard: user-submitted URLs must never make us hit private networks."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise BlockedURL("unsupported URL")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(parts.hostname, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise BlockedURL(f"cannot resolve {parts.hostname}") from exc
    for info in infos:
        if not ipaddress.ip_address(info[4][0].split("%")[0]).is_global:
            raise BlockedURL(f"{parts.hostname} is not a public host")
