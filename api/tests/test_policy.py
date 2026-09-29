from app.schemas import WebcamSummary
from webcam_policy import host_of, is_blocked, is_commercial


def row(**kw):
    base = {"id": 7, "name": "x", "latitude": 1.0, "longitude": 2.0, "country_code": "FR", "region": None,
            "city": None, "is_live": True, "status": "approved", "preview_type": None, "preview_url": None,
            "preview_live": None, "preview_resolver": None, "embed_url": None, "page_url": None}
    return base | kw


def test_policy_helpers():
    assert host_of("https://www.Example.com:8080/a") == "example.com"
    assert is_commercial("https://champery.roundshot.com/cams/1/default")
    assert is_commercial("https://www.trinum.com/ibox/ftpcam/x.jpg")
    assert not is_commercial("http://82.12.3.4/axis-cgi/jpg/image.cgi")
    assert is_blocked("https://cams.hotel.fr/a.jpg", {"hotel.fr"})
    assert not is_blocked("https://nothotel.fr/a.jpg", {"hotel.fr"})


def test_open_camera_goes_through_proxy():
    p = WebcamSummary.from_row(row(preview_type="image", preview_url="http://82.12.3.4/snap.jpg")).preview
    assert p.proxied and p.url.endswith("/webcams/7/snapshot")


def test_commercial_image_is_loaded_from_the_provider():
    url = "https://champery.roundshot.com/cams/1226/default"
    p = WebcamSummary.from_row(row(preview_type="image", preview_url=url)).preview
    assert not p.proxied and p.url == url


def test_commercial_resolver_image_falls_back_to_player():
    r = row(preview_type="image", preview_url="https://www.skaping.com/a/b", preview_resolver="skaping",
            embed_url="https://www.skaping.com/a/b")
    cam = WebcamSummary.from_row(r)
    assert cam.preview is None and cam.embed_url
