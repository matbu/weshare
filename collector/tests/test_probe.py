import asyncio
import io

import httpx
from PIL import Image

from app import probe
from app.jobs.discovery import extract_candidates
from app.jobs.health import Check, check_stream, evaluate
from app.media import classify_url
from app.polite import Fetched


def jpeg(color=(10, 20, 30), size=(320, 180), noise=False) -> bytes:
    buf = io.BytesIO()
    img = Image.effect_noise(size, 100).convert("RGB") if noise else Image.new("RGB", size, color)
    img.save(buf, "JPEG", quality=95)
    return buf.getvalue()


def mjpeg_stream(frames=2) -> bytes:
    parts = [b"--myboundary\r\nContent-Type: image/jpeg\r\n\r\n" + jpeg((i * 40, 0, 0)) + b"\r\n" for i in range(frames)]
    return b"".join(parts)


MP4 = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"\x00" * 64
TS = bytes([0x47] + [0] * 187) * 4
MPD_LIVE = b'<?xml version="1.0"?><MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="dynamic"></MPD>'
MPD_VOD = b'<?xml version="1.0"?><MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static"></MPD>'
MASTER = b"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=800000\nlow/index.m3u8\n"
MEDIA_LIVE = b"#EXTM3U\n#EXT-X-TARGETDURATION:4\n#EXTINF:4.0,\nseg1.ts\n#EXTINF:4.0,\nseg2.ts\n"
MEDIA_VOD = MEDIA_LIVE + b"#EXT-X-ENDLIST\n"


class FakeClient:
    def __init__(self, routes):
        self.routes = routes  # url -> (status, content_type, body)

    async def fetch(self, url, until=None, **kwargs):
        status, ctype, body = self.routes.get(url, (404, "text/html", b"nope"))
        truncated = False
        if until:  # emulate chunked reading with early stop
            for end in range(4096, len(body) + 4096, 4096):
                if until(body[:end]):
                    body, truncated = body[:end], True
                    break
        return Fetched(url, status, httpx.Headers({"content-type": ctype}), body, truncated)


def run(coro):
    return asyncio.run(coro)


def test_sniff_by_content_type_and_signature():
    assert probe.sniff("image/jpeg", jpeg()) == "image"
    assert probe.sniff("multipart/x-mixed-replace; boundary=myboundary", b"") == "mjpeg"
    assert probe.sniff("text/plain", mjpeg_stream()) == "mjpeg"        # wrong Content-Type
    assert probe.sniff("application/octet-stream", MEDIA_LIVE) == "hls"
    assert probe.sniff("application/dash+xml", b"") == "dash"
    assert probe.sniff("text/xml", MPD_LIVE) == "dash"
    assert probe.sniff("application/octet-stream", MP4) == "mp4"
    assert probe.sniff("application/octet-stream", TS) == "ts"
    assert probe.sniff("text/html", b"<html><body>hello</body></html>") is None


def test_probe_mjpeg_needs_two_frames():
    one = FakeClient({"u": (200, "multipart/x-mixed-replace; boundary=myboundary", mjpeg_stream(1))})
    assert run(probe.probe(one, "u")).kind is None
    two = FakeClient({"u": (200, "multipart/x-mixed-replace; boundary=myboundary", mjpeg_stream(5))})
    assert run(probe.probe(two, "u")) == probe.Probe("mjpeg", live=True)


def test_probe_hls_follows_master_to_segment_and_detects_vod():
    routes = {
        "https://s/live.m3u8": (200, "application/vnd.apple.mpegurl", MASTER),
        "https://s/low/index.m3u8": (200, "application/vnd.apple.mpegurl", MEDIA_LIVE),
        "https://s/low/seg1.ts": (200, "video/mp2t", TS),
    }
    assert run(probe.probe(FakeClient(routes), "https://s/live.m3u8")) == probe.Probe("hls", live=True)
    routes["https://s/low/index.m3u8"] = (200, "application/vnd.apple.mpegurl", MEDIA_VOD)
    assert run(probe.probe(FakeClient(routes), "https://s/live.m3u8")) == probe.Probe("hls", live=False)
    del routes["https://s/low/seg1.ts"]
    assert "segment" in run(probe.probe(FakeClient(routes), "https://s/live.m3u8")).error


def test_probe_dash_mp4_ts():
    assert run(probe.probe(FakeClient({"u": (200, "application/dash+xml", MPD_LIVE)}), "u")) == probe.Probe("dash", True)
    assert run(probe.probe(FakeClient({"u": (200, "application/dash+xml", MPD_VOD)}), "u")) == probe.Probe("dash", False)
    assert run(probe.probe(FakeClient({"u": (200, "video/mp4", MP4)}), "u")) == probe.Probe("mp4", False)
    assert run(probe.probe(FakeClient({"u": (200, "video/mp2t", TS)}), "u")).kind is None


def test_mjpeg_endpoint_serving_a_single_jpeg_is_reclassified():
    client = FakeClient({"http://cam/video.cgi": (200, "image/jpeg", jpeg())})
    chk = run(check_stream(client, {"url": "http://cam/video.cgi", "type": "mjpeg"}))
    assert not chk.reachable and chk.kind == "image"
    ep = {"id": 1, "type": "mjpeg", "content_hash": None, "last_change": None, "fail_count": 3}
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    state = evaluate(ep, chk, now)
    assert state["next_check_at"] <= now and state["fail_count"] == 0  # re-checked at once as an image


def test_live_flag_is_stored():
    from datetime import datetime, timezone
    ep = {"id": 1, "type": "hls", "content_hash": None, "last_change": None, "fail_count": 0}
    state = evaluate(ep, Check(True, 200, live=False), datetime.now(timezone.utc))
    assert state["ok"] and state["live"] is False


def test_classify_and_video_tags():
    assert classify_url("https://cdn.example.com/cam/manifest.mpd") == "dash"
    assert classify_url("https://cdn.example.com/cam/last-24h.mp4") == "mp4"
    html = """
      <video src="/stream/live"></video>
      <video><source src="/hls/cam" type="application/x-mpegURL"></video>
      <video><source src="/timelapse.webm" type="video/webm"></video>
    """
    assert extract_candidates(html, "https://site.org/cam") == [
        ("mp4", "https://site.org/stream/live"),   # unknown extension: probed later
        ("hls", "https://site.org/hls/cam"),
        ("mp4", "https://site.org/timelapse.webm"),
    ]


def test_probe_mjpeg_with_large_frames():
    frame = jpeg(size=(1280, 720), noise=True)
    assert len(frame) > 200_000  # a single frame is far above the 64 KB early-stop threshold
    stream = b"".join(b"--b\r\nContent-Type: image/jpeg\r\n\r\n" + frame + b"\r\n" for _ in range(3))
    client = FakeClient({"u": (200, "multipart/x-mixed-replace; boundary=b", stream)})
    assert run(probe.probe(client, "u")) == probe.Probe("mjpeg", live=True)
