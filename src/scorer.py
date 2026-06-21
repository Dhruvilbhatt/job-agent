from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from anthropic import AsyncAnthropic

from .config import Config
from .profile import Profile
from .sources.base import JobPosting

log = logging.getLogger(__name__)
PROMPTS = Path(__file__).resolve().parent / "prompts"


@dataclass
class ScoredJob:
    job: JobPosting
    score: int
    fit_summary: str
    match_reasons: list[str]
    concerns: list[str]


def _read_prompt(name: str) -> str:
    return (PROMPTS / name).read_text()


def _extract_json(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        raise ValueError(f"No JSON object in response: {text[:200]}")
    return json.loads(m.group(0))


def _recency_label(posted_at: datetime | None) -> str:
    if posted_at is None:
        return "unknown"
    if posted_at.tzinfo is None:
        posted_at = posted_at.replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - posted_at
    days = delta.days
    if days <= 0:
        return "today"
    if days == 1:
        return "1 day ago"
    return f"{days} days ago"


def _days_old(posted_at: datetime | None) -> int | None:
    if posted_at is None:
        return None
    if posted_at.tzinfo is None:
        posted_at = posted_at.replace(tzinfo=timezone.utc)
    return max(0, (datetime.now(timezone.utc) - posted_at).days)


async def _stage1(
    client: AsyncAnthropic,
    cfg: Config,
    profile_block: str,
    job: JobPosting,
) -> tuple[JobPosting, bool, int]:
    instructions = _read_prompt("filter.txt")
    user = (
        f"Title: {job.title}\n"
        f"Company: {job.company}\n"
        f"Location: {job.location}\n"
        f"Posted: {_recency_label(job.posted_at)}\n\n"
        f"Description excerpt:\n{job.description[:800]}"
    )
    try:
        resp = await client.messages.create(
            model=cfg.model_filter,
            max_tokens=200,
            system=[
                {"type": "text", "text": instructions},
                {"type": "text", "text": profile_block, "cache_control": {"type": "ephemeral"}},
            ],
            messages=[{"role": "user", "content": user}],
        )
        data = _extract_json(resp.content[0].text)
        return job, bool(data.get("pass", False)), int(data.get("rough_score", 0))
    except Exception as e:
        log.warning("stage1 failed for %s @ %s: %s", job.title, job.company, e)
        return job, False, 0


async def _stage2(
    client: AsyncAnthropic,
    cfg: Config,
    profile_block: str,
    job: JobPosting,
) -> ScoredJob | None:
    instructions = _read_prompt("score.txt")
    user = (
        f"Title: {job.title}\n"
        f"Company: {job.company}\n"
        f"Location: {job.location}\n"
        f"Remote: {job.remote}\n"
        f"Posted: {_recency_label(job.posted_at)}\n"
        f"Source: {job.source}\n"
        f"URL: {job.url}\n\n"
        f"Full description:\n{job.description}"
    )
    try:
        resp = await client.messages.create(
            model=cfg.model_scorer,
            max_tokens=600,
            system=[
                {"type": "text", "text": instructions},
                {"type": "text", "text": profile_block, "cache_control": {"type": "ephemeral"}},
            ],
            messages=[{"role": "user", "content": user}],
        )
        data = _extract_json(resp.content[0].text)
        return ScoredJob(
            job=job,
            score=int(data.get("score", 0)),
            fit_summary=str(data.get("fit_summary", ""))[:600],
            match_reasons=list(data.get("match_reasons", []))[:4],
            concerns=list(data.get("concerns", []))[:4],
        )
    except Exception as e:
        log.warning("stage2 failed for %s @ %s: %s", job.title, job.company, e)
        return None


async def score_jobs(
    cfg: Config,
    profile: Profile,
    jobs: list[JobPosting],
    *,
    stage1_concurrency: int = 8,
    stage2_concurrency: int = 4,
) -> list[ScoredJob]:
    client = AsyncAnthropic(api_key=cfg.anthropic_api_key)
    profile_block = profile.as_prompt_block()

    sem1 = asyncio.Semaphore(stage1_concurrency)

    async def s1(j):
        async with sem1:
            return await _stage1(client, cfg, profile_block, j)

    s1_results = await asyncio.gather(*[s1(j) for j in jobs])
    survivors = [job for job, passed, _ in s1_results if passed]
    log.info("stage1: %d/%d jobs passed pre-filter", len(survivors), len(jobs))

    sem2 = asyncio.Semaphore(stage2_concurrency)

    async def s2(j):
        async with sem2:
            return await _stage2(client, cfg, profile_block, j)

    s2_results = await asyncio.gather(*[s2(j) for j in survivors])
    scored = [r for r in s2_results if r is not None]
    # Sort by score, break ties on freshness (newer first).
    scored.sort(
        key=lambda x: (
            x.score,
            -(_days_old(x.job.posted_at) if x.job.posted_at else 99),
        ),
        reverse=True,
    )
    log.info("stage2: scored %d jobs", len(scored))
    return scored


async def build_digest_html(
    cfg: Config,
    profile: Profile,
    scored: list[ScoredJob],
    slot: str,
) -> str:
    client = AsyncAnthropic(api_key=cfg.anthropic_api_key)
    instructions = _read_prompt("digest.txt")

    payload = {
        "slot": slot,
        "jobs": [
            {
                "company": s.job.company,
                "title": s.job.title,
                "location": s.job.location,
                "remote": s.job.remote,
                "url": s.job.url,
                "source": s.job.source,
                "posted": _recency_label(s.job.posted_at),
                "days_old": _days_old(s.job.posted_at),
                "score": s.score,
                "fit_summary": s.fit_summary,
                "match_reasons": s.match_reasons,
                "concerns": s.concerns,
            }
            for s in scored
        ],
    }

    resp = await client.messages.create(
        model=cfg.model_digest,
        max_tokens=8000,
        system=[
            {"type": "text", "text": instructions},
            {"type": "text", "text": profile.as_prompt_block(), "cache_control": {"type": "ephemeral"}},
        ],
        messages=[{"role": "user", "content": json.dumps(payload, indent=2)}],
    )
    html = resp.content[0].text.strip()
    html = re.sub(r"^```(?:html)?\s*", "", html)
    html = re.sub(r"\s*```$", "", html)
    return html
