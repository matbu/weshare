from datetime import datetime, timedelta, timezone

from app.jobs.discovery import extract_candidates
from app.jobs.health import Check, evaluate
from app.media import classify_url, sniff_image, split_tag_urls
from app.sources import osm, windy


def test_classify_url():
    assert classify_url("http://cam.example.com/webcam/current.jpg") == "image"
    assert classify_url("http://1.2.3.4/axis-cgi/jpg/image.cgi?resolution=640x480") == "image"
    assert classify_url("http://1.2.3.4/axis-cgi/mjpg/video.cgi") == "mjpeg"
    assert classify_url("https://stream.example.com/live/cam1/index.m3u8") == "hls"
    assert classify_url("https://www.youtube.com/watch?v=abc") == "youtube"
    assert classify_url("https://www.example.com/webcams") == "page"


def test_split_tag_urls():
    assert split_tag_urls("www.a.com/cam; https://b.org/x.jpg ;junk") == [
        "http://www.a.com/cam",
        "https://b.org/x.jpg",
    ]
    assert split_tag_urls(None) == []


def test_sniff_image():
    assert sniff_image(b"\xff\xd8\xff\xe0rest") == "image/jpeg"
    assert sniff_image(b"RIFF1234WEBPVP8 ") == "image/webp"
    assert sniff_image(b"<html>") is None


def test_osm_extract_node():
    rec = osm.extract({
        "type": "node", "id": 42, "lat": 41.3871, "lon": 9.1562,
        "tags": {
            "man_made": "surveillance", "surveillance": "webcam", "name": "Port de Bonifacio",
            "contact:webcam": "https://example.com/webcam;https://example.com/cam/current.jpg",
            "website": "https://example.com",
        },
    })
    assert rec.source_id == "node/42"
    assert rec.name == "Port de Bonifacio"
    assert [(e.type, e.url) for e in rec.endpoints] == [
        ("page", "https://example.com/webcam"),
        ("image", "https://example.com/cam/current.jpg"),
        ("page", "https://example.com"),
    ]
    assert rec.webpage_url == "https://example.com/webcam"


def test_osm_extract_way_uses_center_and_ignores_poi_website():
    rec = osm.extract({
        "type": "way", "id": 7, "center": {"lat": 45.0, "lon": 6.0},
        "tags": {"tourism": "hotel", "contact:webcam": "https://h.com/cam.jpg", "website": "https://h.com"},
    })
    assert (rec.latitude, rec.longitude) == (45.0, 6.0)
    assert [e.url for e in rec.endpoints] == ["https://h.com/cam.jpg"]


def test_osm_extract_without_coordinates():
    assert osm.extract({"type": "relation", "id": 1, "tags": {}}) is None


def test_world_tiles_cover_the_globe():
    tiles = osm.world_tiles()
    assert len(tiles) == 72
    assert min(t[0] for t in tiles) == -90 and max(t[2] for t in tiles) == 90
    assert sorted(osm.split((0, 0, 10, 10)))[0] == (0, 0, 5.0, 5.0)


def test_windy_extract():
    rec = windy.extract({
        "webcamId": 123, "title": "Bonifacio Marina",
        "location": {"latitude": 41.3872, "longitude": 9.1564, "country_code": "FR", "city": "Bonifacio"},
        "player": {"day": "https://webcams.windy.com/webcams/public/embed/player/123/day"},
        "urls": {"detail": "https://www.windy.com/webcams/123"},
    })
    assert rec.source_id == "123"
    assert rec.endpoints[0].type == "iframe"
    assert rec.country_code == "FR"


def test_discovery_candidates():
    html = """
    <html><head><meta property="og:image" content="/img/logo.png"></head><body>
      <img src="/images/header.jpg">
      <img src="/webcam/current.jpg?t=1">
      <img data-src="https://cdn.example.com/cam2/latest.jpg">
      <iframe src="https://www.youtube.com/embed/xyz"></iframe>
      <script>var src = "https://stream.example.com/live/playlist.m3u8";</script>
    </body></html>
    """
    found = extract_candidates(html, "https://example.com/page")
    assert found == [
        ("hls", "https://stream.example.com/live/playlist.m3u8"),
        ("youtube", "https://www.youtube.com/embed/xyz"),
        ("image", "https://example.com/webcam/current.jpg?t=1"),
    ]


def _ep(**kw):
    base = {"id": 1, "type": "image", "content_hash": None, "last_change": None, "fail_count": 0}
    return base | kw


def test_evaluate_first_success():
    now = datetime.now(timezone.utc)
    state = evaluate(_ep(), Check(True, 200, content_hash="a"), now)
    assert state["ok"] and state["last_change"] == now and state["fail_count"] == 0
    assert state["next_check_at"] > now + timedelta(hours=5)


def test_evaluate_frozen_image():
    now = datetime.now(timezone.utc)
    old = now - timedelta(days=3)
    state = evaluate(_ep(content_hash="a", last_change=old), Check(True, 200, content_hash="a"), now)
    assert not state["ok"] and state["error"] == "frozen image"
    state = evaluate(_ep(content_hash="a", last_change=old), Check(True, 200, content_hash="b"), now)
    assert state["ok"] and state["last_change"] == now


def test_evaluate_failure_backoff():
    now = datetime.now(timezone.utc)
    state = evaluate(_ep(fail_count=5), Check(False, 503, "HTTP 503"), now)
    assert not state["ok"] and state["fail_count"] == 6
    assert timedelta(hours=14) < state["next_check_at"] - now < timedelta(hours=20)
    state = evaluate(_ep(fail_count=40), Check(False, error="dns"), now)
    assert state["next_check_at"] - now <= timedelta(days=7) * 1.2


def test_find_by_url_ignores_pages():
    from app import store
    assert "type NOT IN ('page', 'iframe')" in store.FIND_BY_URL


def test_evaluate_old_last_modified_is_frozen_immediately():
    now = datetime.now(timezone.utc)
    old = Check(True, 200, content_hash="a", last_modified="Wed, 28 May 2025 13:40:28 GMT")
    state = evaluate(_ep(), old, now)
    assert not state["ok"] and state["error"] == "frozen image"
    recent = Check(True, 200, content_hash="a", last_modified=now.strftime("%a, %d %b %Y %H:%M:%S GMT"))
    assert evaluate(_ep(), recent, now)["ok"]
