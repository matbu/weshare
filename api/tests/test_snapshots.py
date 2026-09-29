import asyncio

import httpx
import pytest
from fastapi import HTTPException

from app import snapshots


class FakePool:
    def __init__(self, row):
        self.row = row

    async def fetchrow(self, *args):
        return self.row


@pytest.fixture
def source(monkeypatch):
    """A fake webcam source: `responses[url]` = list of (status, headers, body) served in order."""
    state = {"responses": {}, "calls": []}

    async def fake_fetch(url, headers=None):
        state["calls"].append((url, dict(headers or {})))
        queue = state["responses"][url]
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, Exception):
            raise item
        status, hdrs, body = item
        return status, httpx.Headers(hdrs), body

    monkeypatch.setattr(snapshots, "_fetch", fake_fetch)
    snapshots._cache.clear()
    return state


def use_endpoint(monkeypatch, url, resolver=None):
    monkeypatch.setattr(snapshots, "get_pool", lambda: FakePool({"url": url, "resolver": resolver}))


def expire(webcam_id):
    snapshots._cache[webcam_id] = snapshots.replace(snapshots._cache[webcam_id], checked_at=-1e9)


JPEG = {"content-type": "image/jpeg", "etag": '"v1"', "last-modified": "Mon, 28 Sep 2026 21:00:00 GMT"}


def test_cached_then_conditional_304(monkeypatch, source):
    use_endpoint(monkeypatch, "https://cam/img.jpg")
    source["responses"]["https://cam/img.jpg"] = [(200, JPEG, b"\xff\xd8img1"), (304, {}, b"")]
    first = asyncio.run(snapshots.get(1))
    again = asyncio.run(snapshots.get(1))
    assert again is first and len(source["calls"]) == 1          # served from cache
    expire(1)
    third = asyncio.run(snapshots.get(1))
    assert source["calls"][1][1] == {"If-None-Match": '"v1"', "If-Modified-Since": JPEG["last-modified"]}
    assert third.body == b"\xff\xd8img1" and third.updated_at == first.updated_at


def test_stale_image_served_when_source_fails(monkeypatch, source):
    use_endpoint(monkeypatch, "https://cam/img.jpg")
    source["responses"]["https://cam/img.jpg"] = [(200, JPEG, b"\xff\xd8img1"), httpx.ConnectError("down")]
    first = asyncio.run(snapshots.get(2))
    expire(2)
    assert asyncio.run(snapshots.get(2)).body == first.body


def test_failure_without_previous_image(monkeypatch, source):
    use_endpoint(monkeypatch, "https://cam/img.jpg")
    source["responses"]["https://cam/img.jpg"] = [(404, {}, b"")]
    with pytest.raises(HTTPException) as exc:
        asyncio.run(snapshots.get(3))
    assert exc.value.status_code == 502


def test_skaping_resolver(monkeypatch, source):
    page = "https://www.skaping.com/valmorel/planchamp"
    image = "https://skaping.s3.gra.io.cloud.ovh.net/valmorel/planchamp/2026/09/28/large/23-00.jpg"
    use_endpoint(monkeypatch, page, resolver="skaping")
    source["responses"][page] = [(200, {}, f'<meta property="og:image" content="{image}">'.encode())]
    source["responses"][image] = [(200, {"content-type": "image/jpeg"}, b"\xff\xd8sk")]
    snap = asyncio.run(snapshots.get(4))
    assert snap.image_url == image and snap.body == b"\xff\xd8sk"
    expire(4)
    asyncio.run(snapshots.get(4))
    assert [u for u, _ in source["calls"]].count(page) == 1        # page re-read at most every 60 s


def test_interval_backs_off_while_unchanged_and_resets_on_change(monkeypatch, source):
    use_endpoint(monkeypatch, "https://cam/img.jpg")
    source["responses"]["https://cam/img.jpg"] = [
        (200, JPEG, b"\xff\xd8a"), (304, {}, b""), (200, JPEG, b"\xff\xd8a"), (304, {}, b""),
        (200, JPEG, b"\xff\xd8b"),
    ]
    intervals = []
    for _ in range(5):
        intervals.append(asyncio.run(snapshots.get(5)).interval)
        expire(5)
    assert intervals == [3.0, 6.0, 12.0, 24.0, 3.0]
    for _ in range(6):
        snapshots._cache[5] = snapshots.replace(snapshots._cache[5], interval=snapshots._slower(snapshots._cache[5]))
    assert snapshots._cache[5].interval == snapshots.MAX_INTERVAL
