"""Collector entry point.

    python -m app.main                      # scheduler (default container command)
    python -m app.main run osm              # one-shot run of a job
    python -m app.main run osm --bbox 41,8,43,10   # one-shot on a bbox (south,west,north,east)
"""

import argparse
import asyncio
import logging
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable

from .config import settings
from .jobs import discovery, health
from .polite import PoliteClient
from .runs import RunTracker, last_run, mark_interrupted_runs
from .sources import osm, windy

log = logging.getLogger("collector")


@dataclass
class Job:
    name: str
    every: timedelta
    fn: Callable[..., Awaitable[None]]
    enabled: bool = True
    accepts_bbox: bool = False


JOBS = {
    j.name: j
    for j in (
        Job("osm", timedelta(hours=settings.osm_every_hours), osm.run, accepts_bbox=True),
        Job("windy", timedelta(hours=settings.windy_every_hours), windy.run,
            enabled=bool(settings.windy_api_key), accepts_bbox=True),
        Job("health", timedelta(minutes=settings.health_every_minutes), health.run),
        Job("discovery", timedelta(minutes=settings.discovery_every_minutes), discovery.run),
    )
}

RETRY_FAILED_AFTER = timedelta(hours=1)


def make_client() -> PoliteClient:
    return PoliteClient(
        settings.user_agent,
        settings.robots_token,
        per_host_interval=settings.per_host_interval,
        concurrency=settings.global_concurrency,
    )


async def run_once(job: Job, client: PoliteClient, **kwargs) -> None:
    async with RunTracker(job.name) as tracker:
        await job.fn(client, tracker, **kwargs)


async def is_due(job: Job) -> bool:
    last = await last_run(job.name)
    if last is None:
        return True
    wait = job.every if last["status"] in ("success", "partial") else min(job.every, RETRY_FAILED_AFTER)
    return datetime.now(timezone.utc) - last["started_at"] >= wait


async def job_loop(job: Job, client: PoliteClient) -> None:
    # Stagger job start-up so everything doesn't fire at the same second.
    await asyncio.sleep(random.uniform(0, 30))
    while True:
        try:
            if await is_due(job):
                await run_once(job, client)
        except Exception:
            log.exception("job %s crashed", job.name)
        await asyncio.sleep(60 + random.uniform(0, 15))


async def scheduler() -> None:
    await mark_interrupted_runs()
    client = make_client()
    enabled = [j for j in JOBS.values() if j.enabled]
    log.info("collector started, jobs: %s", ", ".join(j.name for j in enabled))
    try:
        await asyncio.gather(*(job_loop(j, client) for j in enabled))
    finally:
        await client.aclose()


async def one_shot(name: str, bbox: str | None) -> None:
    job = JOBS[name]
    kwargs = {}
    if bbox:
        if not job.accepts_bbox:
            raise SystemExit(f"job {name} does not accept --bbox")
        kwargs["bbox"] = tuple(float(x) for x in bbox.split(","))
    client = make_client()
    try:
        await run_once(job, client, **kwargs)
    finally:
        await client.aclose()


def main() -> None:
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd")
    run = sub.add_parser("run")
    run.add_argument("job", choices=sorted(JOBS))
    run.add_argument("--bbox", help="south,west,north,east")
    args = parser.parse_args()

    if args.cmd == "run":
        asyncio.run(one_shot(args.job, args.bbox))
    else:
        asyncio.run(scheduler())


if __name__ == "__main__":
    main()
