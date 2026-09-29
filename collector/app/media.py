"""Guess what kind of media a URL points to, without fetching it."""

import re
from urllib.parse import urlsplit

IMAGE_EXT = (".jpg", ".jpeg", ".png", ".webp", ".gif")
IMAGE_PATTERNS = re.compile(
    r"(snapshot\.cgi|image\.cgi|/jpg/image|axis-cgi/jpg|/snap\.jpe?g|cgi-bin/.*snap|"
    r"/webcapture\.jpg|/tmpfs/auto\.jpg|/image/jpeg\.cgi)",
    re.I,
)
MJPEG_PATTERNS = re.compile(r"(mjpe?g|axis-cgi/mjpg|video\.cgi|faststream|/nphmotionjpeg)", re.I)
YOUTUBE_HOSTS = ("youtube.com", "youtu.be", "youtube-nocookie.com")
VIDEO_EXT = (".mp4", ".m4v", ".mov", ".webm")

# MIME types of <video>/<source type="..."> and HTTP Content-Type -> endpoint type
MIME_TYPES = {
    "application/vnd.apple.mpegurl": "hls",
    "application/x-mpegurl": "hls",
    "audio/mpegurl": "hls",
    "application/dash+xml": "dash",
    "video/mp4": "mp4",
    "video/webm": "mp4",
    "video/quicktime": "mp4",
    "multipart/x-mixed-replace": "mjpeg",
}


def mime_to_type(mime: str | None) -> str | None:
    return MIME_TYPES.get((mime or "").split(";")[0].strip().lower())

# Magic bytes of image formats we accept as webcam snapshots.
IMAGE_MAGIC = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)


def normalize_url(value: str | None) -> str | None:
    if not value:
        return None
    url = value.strip().strip("<>\"'")
    if not url or " " in url:
        return None
    if url.startswith("//"):
        url = "https:" + url
    elif url.lower().startswith("www."):
        url = "http://" + url
    if not url.lower().startswith(("http://", "https://")):
        return None
    parts = urlsplit(url)
    if not parts.hostname or "." not in parts.hostname:
        return None
    return url


def split_tag_urls(value: str | None) -> list[str]:
    """OSM allows several values separated by ';'."""
    if not value:
        return []
    return [u for u in (normalize_url(v) for v in value.split(";")) if u]


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
    if path.endswith(VIDEO_EXT):
        return "mp4"
    if MJPEG_PATTERNS.search(full):
        return "mjpeg"
    if path.endswith(IMAGE_EXT) or IMAGE_PATTERNS.search(full):
        return "image"
    return "page"


def sniff_image(body: bytes) -> str | None:
    for magic, mime in IMAGE_MAGIC:
        if body.startswith(magic):
            return mime
    if body[:4] == b"RIFF" and body[8:12] == b"WEBP":
        return "image/webp"
    return None
