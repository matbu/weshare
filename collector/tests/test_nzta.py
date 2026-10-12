from app.sources import nzta
from xml.etree import ElementTree

SAMPLE = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<cameras>
  <camera>
    <description>North along Hinds Highway</description>
    <direction>Northbound</direction>
    <highway>SH1</highway>
    <id>831</id>
    <imageUrl>/camera/831.jpg</imageUrl>
    <latitude>-43.919508</latitude>
    <longitude>171.721221</longitude>
    <name>SH1 Tinwald North</name>
    <offline>true</offline>
    <region><id>11</id><name>Canterbury</name></region>
    <viewUrl>/webcam/831</viewUrl>
  </camera>
  <camera><id>9</id><name>no coords</name><imageUrl>/camera/9.jpg</imageUrl></camera>
</cameras>"""


def test_nzta_extract():
    cams = list(ElementTree.fromstring(SAMPLE).iter("camera"))
    rec = nzta.extract(cams[0])
    assert rec.source == "nzta" and rec.source_id == "831"
    assert rec.name == "SH1 Tinwald North" and rec.country_code == "NZ" and rec.region == "Canterbury"
    assert rec.endpoints == [nzta.store.Endpoint("image", "https://trafficnz.info/camera/831.jpg")]
    assert rec.source_url == "https://trafficnz.info/webcam/831"
    assert nzta.extract(cams[1]) is None  # no coordinates
