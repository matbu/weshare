import json
import logging
from collections import Counter
from datetime import datetime

from . import db

log = logging.getLogger(__name__)

COUNTERS = ("discovered", "inserted", "updated", "duplicates", "errors")


class RunTracker:
    """Records one execution of a job in source_runs (visible live, autocommit)."""

    def __init__(self, source: str):
        self.source = source
        self.counts: Counter = Counter()
        self.run_id: int | None = None
        self.started_at: datetime | None = None

    def incr(self, key: str, n: int = 1) -> None:
        self.counts[key] += n

    async def __aenter__(self) -> "RunTracker":
        self._conn = await db.connect(autocommit=True)
        cur = await self._conn.execute(
            "INSERT INTO source_runs (source) VALUES (%s) RETURNING id, started_at",
            (self.source,),
        )
        row = await cur.fetchone()
        self.run_id, self.started_at = row["id"], row["started_at"]
        log.info("[%s] run %s started", self.source, self.run_id)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        if exc_type:
            status = "failed"
        elif self.counts["errors"]:
            status = "partial"
        else:
            status = "success"
        extra = {k: v for k, v in self.counts.items() if k not in COUNTERS}
        try:
            await self._conn.execute(
                """
                UPDATE source_runs SET
                    finished_at = now(),
                    discovered = %s, inserted = %s, updated = %s, duplicates = %s, errors = %s,
                    stats = %s, status = %s, error = %s
                WHERE id = %s
                """,
                (
                    *(self.counts[k] for k in COUNTERS),
                    json.dumps(extra),
                    status,
                    repr(exc)[:2000] if exc else None,
                    self.run_id,
                ),
            )
        finally:
            await self._conn.close()
        log.info("[%s] run %s %s: %s", self.source, self.run_id, status, dict(self.counts))
        return False


async def mark_interrupted_runs() -> None:
    """Runs left 'running' by a previous container that crashed or was restarted."""
    async with await db.connect(autocommit=True) as conn:
        await conn.execute(
            "UPDATE source_runs SET status = 'aborted', finished_at = now() "
            "WHERE status = 'running'"
        )


async def last_run(source: str) -> dict | None:
    async with await db.connect(autocommit=True) as conn:
        cur = await conn.execute(
            "SELECT started_at, status FROM source_runs WHERE source = %s "
            "ORDER BY started_at DESC LIMIT 1",
            (source,),
        )
        return await cur.fetchone()
