from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import httpx
from jobspy import scrape_jobs

from .base import JobPosting, JobSource

log = logging.getLogger(__name__)


class JobSpySource(JobSource):
    """Aggregator covering LinkedIn, Indeed, Glassdoor, and Google Jobs via python-jobspy.

    JobSpy scrapes these sites synchronously. We run one query per search term, parallelized
    with a small semaphore to stay under aggregator rate limits.
    """

    name = "jobspy"
    SITES = ["linkedin", "indeed", "glassdoor", "google"]

    def __init__(
        self,
        queries: list[str],
        location: str = "United States",
        hours_old: int = 168,
        results_per_query: int = 20,
        concurrency: int = 2,
    ) -> None:
        self.queries = queries
        self.location = location
        self.hours_old = hours_old
        self.results_per_query = results_per_query
        self.concurrency = concurrency

    async def fetch(self, client: httpx.AsyncClient) -> list[JobPosting]:
        if not self.queries:
            log.info("jobspy: no queries configured; skipping")
            return []

        sem = asyncio.Semaphore(self.concurrency)

        async def one(q: str) -> list[JobPosting]:
            async with sem:
                return await asyncio.to_thread(self._scrape, q)

        batches = await asyncio.gather(*[one(q) for q in self.queries])
        all_jobs = [j for batch in batches for j in batch]
        log.info(
            "jobspy: %d total postings across %d queries (location=%r, hours_old=%d)",
            len(all_jobs),
            len(self.queries),
            self.location,
            self.hours_old,
        )
        return all_jobs

    def _scrape(self, query: str) -> list[JobPosting]:
        try:
            df = scrape_jobs(
                site_name=self.SITES,
                search_term=query,
                location=self.location,
                results_wanted=self.results_per_query,
                hours_old=self.hours_old,
                country_indeed="USA",
                verbose=0,
            )
        except Exception as e:
            log.warning("jobspy query %r failed: %s", query, e)
            return []

        if df is None or df.empty:
            log.info("jobspy query %r returned 0 rows", query)
            return []

        out: list[JobPosting] = []
        for _, row in df.iterrows():
            try:
                site = str(row.get("site") or "jobspy")
                description = row.get("description")
                if description is None or (isinstance(description, float)):
                    description = ""

                posted = _parse_date(row.get("date_posted"))

                url = (
                    row.get("job_url")
                    or row.get("job_url_direct")
                    or ""
                )
                ext_id = f"{site}:{row.get('id') or url}"

                out.append(
                    JobPosting(
                        source=site,
                        company=str(row.get("company") or "").strip(),
                        title=str(row.get("title") or "").strip(),
                        location=str(row.get("location") or "").strip(),
                        url=str(url),
                        description=str(description)[:8000],
                        remote=bool(row.get("is_remote") or False),
                        posted_at=posted,
                        external_id=ext_id,
                    )
                )
            except Exception as e:
                log.warning("jobspy row parse failed: %s", e)
                continue

        log.info("jobspy query %r: %d postings", query, len(out))
        return out


def _parse_date(value) -> datetime | None:
    if value is None:
        return None
    try:
        # pandas sometimes hands us Timestamp, sometimes string, sometimes NaT
        if hasattr(value, "to_pydatetime"):
            dt = value.to_pydatetime()
        else:
            dt = datetime.fromisoformat(str(value))
    except (ValueError, TypeError, AttributeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt
