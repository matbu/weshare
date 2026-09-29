"""Which webcams we may re-serve ourselves, shared by the API and the collector.

Our snapshot proxy downloads and re-serves images: fine for public / open-data cameras, but
commercial webcam providers sell exactly that service. Their images are therefore never
proxied: we show their own player (iframe) or let the browser load the image from them.
"""

from urllib.parse import urlsplit

# Companies whose business is hosting / selling webcam images (and aggregators with terms).
COMMERCIAL_PROVIDERS = (
    "roundshot.com",
    "skaping.com",
    "trinum.com",
    "webcam-hd.com",
    "windy.com",
    "webcamtaxi.com",
    "skylinewebcams.com",
    "earthcam.com",
    "feratel.com",
    "panomax.com",
    "bergfex.com",
    "bergfex.at",
    "bergfex.ch",
    "foto-webcam.eu",
    "livecam.at",
    "yellow.camera",
)


def host_of(url: str | None) -> str:
    return (urlsplit(url or "").hostname or "").lower().removeprefix("www.")


def matches(host: str, domains) -> bool:
    return any(host == d or host.endswith("." + d) for d in domains)


def is_commercial(url: str | None) -> bool:
    return matches(host_of(url), COMMERCIAL_PROVIDERS)


def is_blocked(url: str | None, blocked_hosts) -> bool:
    return matches(host_of(url), blocked_hosts)
