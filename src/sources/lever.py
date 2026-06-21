from __future__ import annotations

import logging
from datetime import datetime, timezone

import httpx

from .base import JobPosting, JobSource, strip_html

log = logging.getLogger(__name__)


class LeverSource(JobSource):
    name = "lever"
    URL = "https://api.lever.co/v0/postings/{slug}?mode=json"

    def __init__(self, companies: list[dict]) -> None:
        self.companies = [c for c in companies if c.get("ats") == "lever"]

    async def fetch(self, client: httpx.AsyncClient) -> list[JobPosting]:
        out: list[JobPosting] = []
        for c in self.companies:
            slug = c["slug"]
            try:
                r = await client.get(self.URL.format(slug=slug), timeout=20)
                r.raise_for_status()
            except Exception as e:
                log.warning("lever %s failed: %s", slug, e)
                continue

            for job in r.json():
                categories = job.get("categories", {}) or {}
                location = categories.get("location", "")
                title = job.get("text", "")
                description = strip_html(job.get("descriptionPlain", "") or job.get("description", ""))
                posted = None
                if ts := job.get("createdAt"):
                    try:
                        posted = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
                    except (TypeError, ValueError):
                        pass

                out.append(
                    JobPosting(
                        source=self.name,
                        company=c["name"],
                        title=title,
                        location=location,
                        url=job.get("hostedUrl", ""),
                        description=description,
                        remote=categories.get("commitment", "").lower() == "remote"
                        or "remote" in location.lower(),
                        posted_at=posted,
                        external_id=str(job.get("id", "")),
                    )
                )
        log.info("lever: fetched %d postings across %d companies", len(out), len(self.companies))
        return out
