"""What does a URL really serve? Decided from the response, never from the URL alone.

`sniff()` is pure (Content-Type + first bytes). `probe()` fetches the URL and, for HLS,
follows the playlist down to one media segment, so a stream is only "working" when
something can actually be played.

| kind  | Content-Type                              | signature                                  |
|-------|-------------------------------------------|--------------------------------------------|
| image | image/*                                   | JPEG FF D8 FF, PNG, GIF, WebP              |
| mjpeg | multipart/x-mixed-replace                 | >= 2 JPEG frames (FF D8 FF)                |
| hls   | application/vnd.apple.mpegurl, *mpegurl   | #EXTM3U                                    |
| dash  | application/dash+xml                      | <MPD                                       |
| mp4   | video/mp4, video/quicktime                | 'ftyp' / 'moov' / 'moof' / 'styp' box at 4 |
| ts    | video/mp2t                                | 0x47 sync byte every 188 bytes             |
"""

import re
from dataclasses import dataclass
from urllib.parse import urljoin

from .media import sniff_image
from .polite import BlockedURL, FetchError, PoliteClient

JPEG_SOI = b"\xff\xd8\xff"
MP4_BOXES = (b"ftyp", b"moov", b"moof", b"styp")
TS_PACKET = 188

# Enough bytes to see two MJPEG frames of a 1080p camera, without downloading the stream.
MJPEG_MAX_BYTES = 2_000_000
PLAYLIST_MAX_BYTES = 1_000_000
SEGMENT_MAX_BYTES = 64_000


@dataclass(frozen=True)
class Probe:
    kind: str | None            # image / mjpeg / hls / dash / mp4 / ts, None if unknown
    live: bool | None = None    # True: live stream, False: recording (VOD), None: not a stream
    error: str | None = None


def _is_ts(body: bytes) -> bool:
    return len(body) >= TS_PACKET * 3 and all(body[i] == 0x47 for i in (0, TS_PACKET, 2 * TS_PACKET))


def sniff(content_type: str, body: bytes) -> str | None:
    ct = (content_type or "").split(";")[0].strip().lower()
    head = body[:2048].lstrip()

    if ct.startswith("multipart/"):
        return "mjpeg"
    if "mpegurl" in ct or head.startswith(b"#EXTM3U"):
        return "hls"
    if ct == "application/dash+xml" or (b"<MPD" in head[:1024]):
        return "dash"
    if ct in ("video/mp4", "video/quicktime", "video/iso.segment") or body[4:8] in MP4_BOXES:
        return "mp4"
    if ct == "video/mp2t" or _is_ts(body):
        return "ts"
    if body.count(JPEG_SOI) >= 2 and b"--" in body[:200]:
        return "mjpeg"  # multipart stream served with a wrong Content-Type
    if sniff_image(body) or ct.startswith("image/"):
        return "image"
    return None


def looks_like_stream(body: bytes) -> bool:
    """Early stop for an 'image' URL that is in fact an endless MJPEG stream."""
    return b"--" in body[:200] and mjpeg_frames(body) >= 2


def mjpeg_frames(body: bytes) -> int:
    return body.count(JPEG_SOI)


def _playlist_lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def first_uri(lines: list[str], after_tag: str | None = None) -> str | None:
    """First URI line, optionally the one following a given tag (e.g. #EXT-X-STREAM-INF)."""
    take = after_tag is None
    for line in lines:
        if line.startswith("#"):
            if after_tag and line.startswith(after_tag):
                take = True
            continue
        if take:
            return line
    return None


def dash_is_live(mpd: str) -> bool:
    return re.search(r"""\btype\s*=\s*["']dynamic["']""", mpd) is not None


async def _probe_hls(client: PoliteClient, url: str, body: bytes, depth: int = 0) -> Probe:
    lines = _playlist_lines(body.decode("utf-8", "replace"))
    if not lines or lines[0] != "#EXTM3U":
        return Probe(None, error="not a HLS playlist")

    if any(line.startswith("#EXT-X-STREAM-INF") for line in lines):  # master playlist
        variant = first_uri(lines, "#EXT-X-STREAM-INF")
        if not variant or depth > 1:
            return Probe(None, error="HLS master playlist without variant")
        variant_url = urljoin(url, variant)
        resp = await client.fetch(variant_url, max_bytes=PLAYLIST_MAX_BYTES, retries=1)
        if resp.status != 200:
            return Probe(None, error=f"HLS variant HTTP {resp.status}")
        return await _probe_hls(client, variant_url, resp.body, depth + 1)

    live = "#EXT-X-ENDLIST" not in lines
    segment = first_uri(lines)
    if not segment:
        return Probe(None, error="HLS playlist without segment")
    resp = await client.fetch(urljoin(url, segment), max_bytes=SEGMENT_MAX_BYTES, retries=1)
    if resp.status not in (200, 206) or not resp.body:
        return Probe(None, error=f"HLS segment HTTP {resp.status}")
    return Probe("hls", live=live)


def _enough(body: bytes) -> bool:
    if mjpeg_frames(body) >= 2:
        return True
    if mjpeg_frames(body):
        return False  # one JPEG so far: a plain image ends by itself, a stream needs frame #2
    head = body[:1024].lstrip()
    is_manifest = head.startswith(b"#EXTM3U") or b"<MPD" in head
    # Neither JPEG nor playlist (read whole: ENDLIST is at the end): 64 KB is enough to sniff.
    return len(body) > 65_536 and not is_manifest


async def probe(client: PoliteClient, url: str) -> Probe:
    """Fetch just enough of `url` to know what it is and whether it plays."""
    try:
        resp = await client.fetch(
            url,
            max_bytes=MJPEG_MAX_BYTES,
            retries=1,
            timeout=20,
            # Stop as soon as the answer is known: never download a whole stream.
            until=_enough,
        )
    except BlockedURL as exc:
        return Probe(None, error=f"blocked: {exc}")
    except FetchError as exc:
        return Probe(None, error=str(exc)[:500])
    if resp.status not in (200, 206):
        return Probe(None, error=f"HTTP {resp.status}")

    kind = sniff(resp.content_type, resp.body)
    try:
        if kind == "mjpeg":
            if mjpeg_frames(resp.body) < 2:
                return Probe(None, error="MJPEG stream sent less than 2 frames")
            return Probe("mjpeg", live=True)
        if kind == "hls":
            return await _probe_hls(client, resp.url, resp.body)
        if kind == "dash":
            return Probe("dash", live=dash_is_live(resp.body.decode("utf-8", "replace")))
        if kind == "mp4":
            return Probe("mp4", live=False)
        if kind == "ts":
            return Probe(None, error="raw MPEG-TS is not playable in browsers")
    except (BlockedURL, FetchError) as exc:
        return Probe(None, error=str(exc)[:500])
    if kind == "image":
        return Probe("image")
    return Probe(None, error=f"unknown media ({resp.content_type or 'no content-type'})")
