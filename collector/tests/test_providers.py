import asyncio
import json

import httpx

from app import providers
from app.polite import Fetched
from app.sources import osm
from webcam_resolvers import resolve


def page(url, html="", headers=None):
    return Fetched(url, 200, httpx.Headers(headers or {"content-type": "text/html"}), html.encode(), False)


class FakeClient:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    async def fetch(self, url, **kwargs):
        self.calls.append(url)
        status, body = self.responses.get(url, (404, ""))
        return Fetched(url, status, httpx.Headers({}), body.encode(), False)


def run(coro):
    return asyncio.run(coro)


SKAPING_HTML = """<meta property="og:image"                 content="https://skaping.s3.gra.io.cloud.ovh.net/valmorel/planchamp/2026/09/28/large/23-00.jpg" />"""


def test_skaping_resolver_and_extractor():
    assert resolve("skaping", SKAPING_HTML, "https://www.skaping.com/valmorel/planchamp").endswith("/large/23-00.jpg")
    found = run(providers.extract(FakeClient({}), page("https://www.skaping.com/valmorel/planchamp", SKAPING_HTML), SKAPING_HTML))
    assert found == [providers.Found("image", "https://www.skaping.com/valmorel/planchamp", resolver="skaping")]


def test_roundshot_extractor():
    html = '<meta property="og:image" content="/cams/1226">'
    found = run(providers.extract(FakeClient({}), page("https://champery.roundshot.com/", html), html))
    assert found == [providers.Found("image", "https://champery.roundshot.com/cams/1226/default")]


def test_webcam_hd_extractor_picks_the_camera():
    group = [
        {"url_part_2": "other", "webcam_display_str_image": "avoriaz_other", "webcam_display_in_group_web_page_tri": 0},
        {"url_part_2": "zore", "webcam_display_str_image": "avoriaz_zore", "webcam_display_in_group_web_page_tri": 1},
    ]
    client = FakeClient({"https://app.webcam-hd.com/smr/json/webcam_display_group/avoriaz.json": (200, json.dumps(group))})
    found = run(providers.extract(client, page("https://app.webcam-hd.com/avoriaz/zore"), ""))
    assert found == [providers.Found("image", "https://www.trinum.com/ibox/ftpcam/avoriaz_zore.jpg")]
    # group page without camera: first camera of the group
    found = run(providers.extract(client, page("https://app.webcam-hd.com/avoriaz"), ""))
    assert found[0].url.endswith("avoriaz_other.jpg")


def test_unknown_provider_returns_none():
    assert run(providers.extract(FakeClient({}), page("https://example.org/cam"), "")) is None


def test_frame_allowed():
    assert providers.frame_allowed(httpx.Headers({}))
    assert not providers.frame_allowed(httpx.Headers({"x-frame-options": "SAMEORIGIN"}))
    assert not providers.frame_allowed(httpx.Headers({"content-security-policy": "frame-ancestors 'self' https://a.com"}))
    assert providers.frame_allowed(httpx.Headers({"content-security-policy": "default-src 'self'; frame-ancestors *"}))


def test_osm_filters_weather_stations():
    pioupiou = {"man_made": "monitoring_station", "contact:webcam": "https://pioupiou.com/fr/309"}
    assert osm.extract({"type": "node", "id": 1, "lat": 1, "lon": 1, "tags": pioupiou}) is None
    beacon = {"surveillance": "webcam", "contact:webcam": "https://www.balisemeteo.com/balise.php?idBalise=1"}
    rec = osm.extract({"type": "node", "id": 2, "lat": 1, "lon": 1, "tags": beacon})
    assert rec is not None and rec.endpoints == []  # a real camera tag is kept, the bogus link dropped
    holfuy = {"man_made": "surveillance", "surveillance:zone": "webcam", "contact:webcam": "https://holfuy.com/fr/weather/690"}
    assert osm.extract({"type": "node", "id": 3, "lat": 1, "lon": 1, "tags": holfuy}) is not None
