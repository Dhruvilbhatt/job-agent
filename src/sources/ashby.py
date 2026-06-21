from __future__ import annotations

import logging
from datetime import datetime

import httpx

from .base import JobPosting, JobSource, strip_html

log = logging.getLogger(__name__)


class AshbySource(JobSource):
    name = "ashby"
    URL = "https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true"

    def __init__(self, companies: list[dict]) -> None:
        self.companies = [c for c in companies if c.get("ats") == "ashby"]

    async def fetch(self, client: httpx.AsyncClient) -> list[JobPosting]:
        out: list[JobPosting] = []
        for c in self.companies:
            slug = c["slug"]
            try:
                r = await client.get(self.URL.format(slug=slug), timeout=20)
                r.raise_for_status()
            except Exception as e:
                log.warning("ashby %s failed: %s", slug, e)
                continue

            for job in r.json().get("jobs", []):
                location = job.get("locationName", "")
                title = job.get("title", "")
                description = strip_html(job.get("descriptionHtml", ""))
                posted = None
                if updated := job.get("publishedAt"):
                    try:
                        posted = datetime.fromisoformat(updated.replace("Z", "+00:00"))
                    except ValueError:
                        pass

                out.append(
                    JobPosting(
                        source=self.name,
                        company=c["name"],
                        title=title,
                        location=location,
                        url=job.get("jobUrl", ""),
                        description=description,
                        remote=bool(job.get("isRemote", False))
                        or "remote" in location.lower(),
                        posted_at=posted,
                        external_id=str(job.get("id", "")),
                    )
                )
        log.info("ashby: fetched %d postings across %d companies", len(out), len(self.companies))
        return out
