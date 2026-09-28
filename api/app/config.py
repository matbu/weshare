import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    database_url: str = os.environ.get(
        "DATABASE_URL", "postgresql://webcams:webcams@localhost:5432/webcams"
    )
    jwt_secret: str = os.environ.get("JWT_SECRET", "dev-secret-change-me")
    jwt_ttl_days: int = int(os.environ.get("JWT_TTL_DAYS", 30))
    user_agent: str = os.environ.get("BOT_USER_AGENT") or "WorldWebcamsBot/1.0"
    cors_origins: tuple[str, ...] = tuple(os.environ.get("CORS_ORIGINS", "*").split(","))
    root_path: str = os.environ.get("ROOT_PATH", "")
    max_pending_per_user: int = int(os.environ.get("MAX_PENDING_PER_USER", 20))


settings = Settings()
