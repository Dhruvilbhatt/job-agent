from __future__ import annotations

import logging
from datetime import datetime

import httpx

from .base import JobPosting, JobSource, strip_html

log = logging.getLogger(__name__)


class GreenhouseSource(JobSource):
    name = "greenhouse"
    URL = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"

    def __init__(self, companies: list[dict]) -> None:
        self.companies = [c for c in companies if c.get("ats") == "greenhouse"]

    async def fetch(self, client: httpx.AsyncClient) -> list[JobPosting]:
        out: list[JobPosting] = []
        for c in self.companies:
            slug = c["slug"]
            try:
                r = await client.get(self.URL.format(slug=slug), timeout=20)
                r.raise_for_status()
            except Exception as e:
                log.warning("greenhouse %s failed: %s", slug, e)
                continue

            for job in r.json().get("jobs", []):
                location = (job.get("location") or {}).get("name", "")
                title = job.get("title", "")
                description = strip_html(job.get("content", ""))
                posted = None
                if updated := job.get("updated_at"):
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
                        url=job.get("absolute_url", ""),
                        description=description,
                        remote="remote" in location.lower(),
                        posted_at=posted,
                        external_id=str(job.get("id", "")),
                    )
                )
        log.info("greenhouse: fetched %d postings across %d companies", len(out), len(self.companies))
        return out
