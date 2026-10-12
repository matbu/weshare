from app.sources import caltrans
from app.store import Endpoint

ENTRY = {"cctv": {
    "index": "1",
    "location": {"locationName": "Hwy 5 at Pocket", "nearbyPlace": "Sacramento", "county": "Sacramento",
                 "longitude": "-121.510528", "latitude": "38.481128", "route": "I-5"},
    "inService": "true",
    "imageData": {
        "streamingVideoURL": "https://wzmedia.dot.ca.gov/D3/5_Pocket.stream/playlist.m3u8",
        "static": {"currentImageURL": "https://cwwp2.dot.ca.gov/data/d3/cctv/image/x/x.jpg"}},
}}


def test_caltrans_extract_image_and_hls():
    rec = caltrans.extract(3, ENTRY)
    assert rec.source_id == "d3/1" and rec.country_code == "US" and rec.region == "California"
    assert rec.city == "Sacramento"
    assert rec.endpoints == [
        Endpoint("image", "https://cwwp2.dot.ca.gov/data/d3/cctv/image/x/x.jpg"),
        Endpoint("hls", "https://wzmedia.dot.ca.gov/D3/5_Pocket.stream/playlist.m3u8"),
    ]


def test_caltrans_skips_without_coords_or_media():
    assert caltrans.extract(3, {"cctv": {"index": "2", "location": {"latitude": "0", "longitude": "0"}}}) is None
    no_media = {"cctv": {"index": "3", "location": {"latitude": "38", "longitude": "-121"}, "imageData": {}}}
    assert caltrans.extract(3, no_media) is None
