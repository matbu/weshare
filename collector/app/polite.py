"""HTTP client that behaves like a good citizen, so that sources don't ban us.

- honest User-Agent with a contact URL (set BOT_USER_AGENT)
- at most one in-flight request per host, spaced by a minimum interval
- global concurrency cap
- exponential backoff with jitter on 429/5xx, honouring Retry-After
- robots.txt (and its Crawl-delay) for anything that looks like crawling
- conditional GETs are left to callers (ETag / If-Modified-Since)
- SSRF guard: user-submitted URLs may never reach private networks
"""

import asyncio
import email.utils
import ipaddress
import logging
import random
import socket
import time
from collections import defaultdict
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpx

log = logging.getLogger(__name__)

RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_RETRY_AFTER = 900.0


class FetchError(Exception):
    pass


class BlockedURL(FetchError):
    pass


@dataclass
class Fetched:
    url: str
    status: int
    headers: httpx.Headers
    body: bytes
    truncated: bool

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "").split(";")[0].strip().lower()

    @property
    def retry_after(self) -> float | None:
        value = self.headers.get("retry-after")
        if not value:
            return None
        if value.strip().isdigit():
            return float(value)
        try:
            when = email.utils.parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        return max(0.0, when.timestamp() - time.time())


async def assert_public_url(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise BlockedURL(f"unsupported URL: {url}")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(
            parts.hostname, port, type=socket.SOCK_STREAM
        )
    except socket.gaierror as exc:
        raise FetchError(f"DNS resolution failed for {parts.hostname}") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if not ip.is_global:
            raise BlockedURL(f"{parts.hostname} resolves to non-public address {ip}")


class PoliteClient:
    def __init__(
        self,
        user_agent: str,
        robots_token: str,
        per_host_interval: float = 3.0,
        concurrency: int = 16,
        timeout: float = 30.0,
    ):
        self._client = httpx.AsyncClient(
            headers={"User-Agent": user_agent},
            timeout=httpx.Timeout(timeout, connect=10.0),
            follow_redirects=False,
            limits=httpx.Limits(max_connections=concurrency * 2),
        )
        self._robots_token = robots_token
        self._default_interval = per_host_interval
        self._host_interval: dict[str, float] = {}
        self._host_next: dict[str, float] = {}
        self._host_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._sem = asyncio.Semaphore(concurrency)
        self._robots: dict[str, RobotFileParser | None] = {}
        self._robots_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def aclose(self) -> None:
        await self._client.aclose()

    def set_host_interval(self, host: str, seconds: float) -> None:
        self._host_interval[host.lower()] = seconds

    def _penalize(self, host: str, delay: float) -> None:
        self._host_next[host] = max(self._host_next.get(host, 0.0), time.monotonic() + delay)

    async def _wait_turn(self, host: str) -> None:
        delay = self._host_next.get(host, 0.0) - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
        interval = self._host_interval.get(host, self._default_interval)
        # Small jitter so we never look like a metronome.
        self._host_next[host] = time.monotonic() + interval * random.uniform(1.0, 1.25)

    async def fetch(
        self,
        url: str,
        *,
        method: str = "GET",
        headers: dict | None = None,
        params: dict | None = None,
        data: str | bytes | None = None,
        max_bytes: int = 5_000_000,
        check_public: bool = True,
        retries: int = 3,
        timeout: float | None = None,
    ) -> Fetched:
        host = urlsplit(url).hostname or ""
        for attempt in range(retries + 1):
            try:
                result = await self._fetch_once(
                    url, method, headers, params, data, max_bytes, check_public, timeout
                )
            except BlockedURL:
                raise
            except (httpx.InvalidURL, httpx.UnsupportedProtocol) as exc:
                raise FetchError(f"invalid URL {url}: {exc}") from exc
            except (httpx.HTTPError, FetchError) as exc:
                if attempt == retries:
                    raise FetchError(f"{type(exc).__name__}: {exc}") from exc
                await self._backoff(host, attempt, None)
                continue

            if result.status in RETRY_STATUSES and attempt < retries:
                retry_after = result.retry_after
                if retry_after is not None and retry_after > MAX_RETRY_AFTER:
                    self._penalize(host, retry_after)
                    return result
                await self._backoff(host, attempt, retry_after)
                continue
            return result
        raise AssertionError("unreachable")

    async def _backoff(self, host: str, attempt: int, retry_after: float | None) -> None:
        delay = retry_after if retry_after is not None else min(300.0, 5.0 * 2**attempt)
        delay *= random.uniform(1.0, 1.3)
        log.info("backing off %.0fs for %s (attempt %d)", delay, host, attempt + 1)
        self._penalize(host, delay)
        await asyncio.sleep(delay)

    async def _fetch_once(
        self, url, method, headers, params, data, max_bytes, check_public, timeout
    ) -> Fetched:
        current = url
        for _ in range(6):
            if check_public:
                await assert_public_url(current)
            host = (urlsplit(current).hostname or "").lower()
            async with self._host_locks[host]:
                await self._wait_turn(host)
                async with self._sem:
                    async with self._client.stream(
                        method,
                        current,
                        headers=headers,
                        params=params,
                        content=data,
                        timeout=timeout or httpx.USE_CLIENT_DEFAULT,
                    ) as resp:
                        location = resp.headers.get("location")
                        if resp.is_redirect and location:
                            current = urljoin(current, location)
                            params = None
                            if method == "POST" and resp.status_code in (301, 302, 303):
                                method, data = "GET", None
                            continue
                        body, truncated = await _read_capped(resp, max_bytes)
                        return Fetched(current, resp.status_code, resp.headers, body, truncated)
        raise FetchError(f"too many redirects: {url}")

    async def allowed_by_robots(self, url: str) -> bool:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        async with self._robots_locks[origin]:
            if origin not in self._robots:
                self._robots[origin] = await self._load_robots(origin)
        parser = self._robots[origin]
        if parser is None:
            return True
        return parser.can_fetch(self._robots_token, url)

    async def _load_robots(self, origin: str) -> RobotFileParser | None:
        parser = RobotFileParser()
        try:
            resp = await self.fetch(f"{origin}/robots.txt", max_bytes=500_000, retries=1)
        except BlockedURL:
            raise
        except FetchError:
            return None  # unreachable host: the real fetch will fail anyway
        if resp.status >= 500:
            parser.disallow_all = True  # RFC 9309: server error => assume full disallow
            return parser
        if resp.status != 200:
            return None  # 4xx: no restrictions
        parser.parse(resp.body.decode("utf-8", "replace").splitlines())
        delay = parser.crawl_delay(self._robots_token)
        if delay:
            host = urlsplit(origin).hostname or ""
            self.set_host_interval(host, max(float(delay), self._default_interval))
        return parser


async def _read_capped(resp: httpx.Response, max_bytes: int) -> tuple[bytes, bool]:
    chunks = []
    size = 0
    async for chunk in resp.aiter_bytes():
        chunks.append(chunk)
        size += len(chunk)
        if size >= max_bytes:
            return b"".join(chunks)[:max_bytes], True
    return b"".join(chunks), False
