from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from .sources.base import JobPosting

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "seen_jobs.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS seen_jobs (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    company TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    score INTEGER,
    emailed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_seen_jobs_first_seen ON seen_jobs(first_seen);
"""


def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.executescript(SCHEMA)
    return c


def insert_new(jobs: list[JobPosting]) -> list[JobPosting]:
    """Insert jobs we haven't seen; return only the ones that were actually new."""
    if not jobs:
        return []
    now = datetime.now(timezone.utc).isoformat()
    new: list[JobPosting] = []
    with closing(_conn()) as c, c:
        for j in jobs:
            cur = c.execute(
                "INSERT OR IGNORE INTO seen_jobs(id, source, company, title, url, first_seen) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (j.id, j.source, j.company, j.title, j.url, now),
            )
            if cur.rowcount > 0:
                new.append(j)
    return new


def mark_emailed(job_ids: list[str], scores: dict[str, int]) -> None:
    if not job_ids:
        return
    now = datetime.now(timezone.utc).isoformat()
    with closing(_conn()) as c, c:
        for jid in job_ids:
            c.execute(
                "UPDATE seen_jobs SET emailed_at = ?, score = ? WHERE id = ?",
                (now, scores.get(jid), jid),
            )


def stats() -> dict[str, int]:
    with closing(_conn()) as c:
        total = c.execute("SELECT COUNT(*) FROM seen_jobs").fetchone()[0]
        emailed = c.execute("SELECT COUNT(*) FROM seen_jobs WHERE emailed_at IS NOT NULL").fetchone()[0]
    return {"total_seen": total, "total_emailed": emailed}
