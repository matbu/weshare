from datetime import datetime
from typing import Literal

from pydantic import BaseModel, EmailStr, Field, HttpUrl, model_validator

from .config import settings


class Preview(BaseModel):
    type: str
    url: str


class WebcamSummary(BaseModel):
    id: int
    name: str | None
    latitude: float
    longitude: float
    country_code: str | None
    region: str | None
    city: str | None
    is_live: bool
    status: str
    preview: Preview | None
    distance_m: float | None = None

    @classmethod
    def from_row(cls, row) -> "WebcamSummary":
        preview = None
        if row["preview_type"]:
            # Images go through our caching proxy: HTTPS, no hotlink issue, 1 hit/min on the source.
            url = (
                f"{settings.root_path}/webcams/{row['id']}/snapshot"
                if row["preview_type"] == "image"
                else row["preview_url"]
            )
            preview = Preview(type=row["preview_type"], url=url)
        return cls(
            id=row["id"],
            name=row["name"],
            latitude=row["latitude"],
            longitude=row["longitude"],
            country_code=row["country_code"],
            region=row["region"],
            city=row["city"],
            is_live=row["is_live"],
            status=row["status"],
            preview=preview,
            distance_m=row.get("distance_m"),
        )


class EndpointOut(BaseModel):
    id: int
    type: str
    url: str
    origin: str
    is_working: bool | None
    width: int | None
    height: int | None
    last_success: datetime | None


class SourceOut(BaseModel):
    source: str
    source_id: str
    source_url: str | None
    webpage_url: str | None


class WebcamDetail(WebcamSummary):
    description: str | None
    endpoints: list[EndpointOut]
    sources: list[SourceOut]
    first_seen: datetime
    submitted_by_me: bool = False


class WebcamSubmission(BaseModel):
    name: str = Field(min_length=2, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    media_url: HttpUrl | None = Field(default=None, description="JPEG snapshot, HLS (.m3u8), MJPEG or YouTube URL")
    page_url: HttpUrl | None = Field(default=None, description="Web page showing the webcam")

    @model_validator(mode="after")
    def need_one_url(self):
        if not self.media_url and not self.page_url:
            raise ValueError("media_url or page_url is required")
        return self


class Moderation(BaseModel):
    action: Literal["approve", "reject"]


class RegisterIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=200)
    display_name: str | None = Field(default=None, max_length=80)


class LoginIn(BaseModel):
    email: EmailStr
    password: str


class UserOut(BaseModel):
    id: int
    email: str
    display_name: str | None
    role: str
    created_at: datetime


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserOut
