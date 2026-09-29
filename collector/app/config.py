import os
from dataclasses import dataclass


def _env_float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


def _env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


@dataclass(frozen=True)
class Settings:
    database_url: str = os.environ.get(
        "DATABASE_URL", "postgresql://webcams:webcams@localhost:5432/webcams"
    )
    user_agent: str = os.environ.get("BOT_USER_AGENT") or (
        "WorldWebcamsBot/1.0"
    )
    # Token matched against robots.txt "User-agent:" lines.
    robots_token: str = "WorldWebcamsBot"

    # Politeness: never more than one request at a time per host, spaced by this delay.
    per_host_interval: float = _env_float("PER_HOST_INTERVAL", 3.0)
    global_concurrency: int = _env_int("GLOBAL_CONCURRENCY", 16)

    overpass_urls: tuple[str, ...] = tuple(
        os.environ.get(
            "OVERPASS_URLS",
            "https://overpass-api.de/api/interpreter,"
            "https://overpass.private.coffee/api/interpreter",
        ).split(",")
    )
    overpass_interval: float = _env_float("OVERPASS_INTERVAL", 10.0)

    windy_api_key: str = os.environ.get("WINDY_API_KEY", "")

    osm_every_hours: float = _env_float("OSM_EVERY_HOURS", 24 * 7)
    windy_every_hours: float = _env_float("WINDY_EVERY_HOURS", 24)
    health_every_minutes: float = _env_float("HEALTH_EVERY_MINUTES", 10)
    health_batch: int = _env_int("HEALTH_BATCH", 400)
    discovery_every_minutes: float = _env_float("DISCOVERY_EVERY_MINUTES", 15)
    discovery_batch: int = _env_int("DISCOVERY_BATCH", 300)

    log_level: str = os.environ.get("LOG_LEVEL", "INFO")


settings = Settings()
