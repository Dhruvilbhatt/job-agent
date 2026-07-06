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
    *,
    desc_chars: int = 3500,
) -> ScoredJob | None:
    instructions = _read_prompt("score.txt")
    # Fit signals (function, seniority, sponsorship, comp) cluster in the first
    # few KB of a JD; the tail is benefits/EEO boilerplate. Truncating here cuts
    # Sonnet input tokens ~50% with negligible impact on scoring quality.
    user = (
        f"Title: {job.title}\n"
        f"Company: {job.company}\n"
        f"Location: {job.location}\n"
        f"Remote: {job.remote}\n"
        f"Posted: {_recency_label(job.posted_at)}\n"
        f"Source: {job.source}\n"
        f"URL: {job.url}\n\n"
        f"Description (truncated):\n{job.description[:desc_chars]}"
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
    rough_score_min: int = 5,
    stage2_max: int = 40,
) -> list[ScoredJob]:
    """Two-stage scoring funnel with a bounded expensive stage.

    Stage 1 (cheap Haiku) filters every job and emits a rough 0-10 score.
    Stage 2 (Sonnet) is the dominant cost, so we bound it: keep only Stage-1
    survivors with rough_score >= ``rough_score_min``, sort by rough_score
    (best first), and deep-score at most ``stage2_max`` of them. Because the
    cap is applied *after* sorting — and sits well above the email's max-picks —
    the strongest candidates are always scored; only marginal tail jobs are
    deferred. This makes per-run Sonnet cost predictable regardless of how many
    postings the boards return on a given day.
    """
    client = AsyncAnthropic(api_key=cfg.anthropic_api_key)
    profile_block = profile.as_prompt_block()

    sem1 = asyncio.Semaphore(stage1_concurrency)

    async def s1(j):
        async with sem1:
            return await _stage1(client, cfg, profile_block, j)

    s1_results = await asyncio.gather(*[s1(j) for j in jobs])
    survivors = [(job, rough) for job, passed, rough in s1_results if passed]
    log.info("stage1: %d/%d jobs passed pre-filter", len(survivors), len(jobs))

    # Gate on Stage-1 confidence, then rank and cap before the expensive stage.
    gated = [(job, rough) for job, rough in survivors if rough >= rough_score_min]
    gated.sort(key=lambda t: t[1], reverse=True)
    to_score = [job for job, _ in gated[:stage2_max]]
    log.info(
        "stage2 gate: %d/%d survivors cleared rough>=%d; deep-scoring top %d with %s",
        len(gated),
        len(survivors),
        rough_score_min,
        len(to_score),
        cfg.model_scorer,
    )

    sem2 = asyncio.Semaphore(stage2_concurrency)

    async def s2(j):
        async with sem2:
            return await _stage2(client, cfg, profile_block, j)

    s2_results = await asyncio.gather(*[s2(j) for j in to_score])
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


_SLOT_LABELS = {"morning": "8 AM", "evening": "5 PM"}


def _esc(s) -> str:
    """Minimal HTML escape — every untrusted string from job boards goes through this."""
    if s is None:
        return ""
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _render_card(s: ScoredJob) -> str:
    j = s.job
    days = _days_old(j.posted_at)
    fresh_badge = (
        '<span style="display:inline-block;background:#dcfce7;color:#15803d;'
        'font-size:11px;font-weight:600;padding:2px 8px;border-radius:99px;'
        'margin-left:8px;">Fresh</span>'
        if days is not None and days <= 1
        else ""
    )
    remote_badge = (
        '<span style="display:inline-block;background:#eef2ff;color:#4338ca;'
        'font-size:11px;font-weight:600;padding:2px 8px;border-radius:99px;'
        'margin-left:6px;">Remote</span>'
        if j.remote
        else ""
    )

    reasons_html = ""
    if s.match_reasons:
        items = "".join(
            f'<li style="margin:4px 0;color:#15803d;font-size:14px;line-height:1.5;">'
            f'<span style="color:#15803d;font-weight:700;">✓</span> {_esc(r)}</li>'
            for r in s.match_reasons
        )
        reasons_html = (
            f'<ul style="list-style:none;padding:0;margin:10px 0 0;">{items}</ul>'
        )

    concerns_html = ""
    if s.concerns:
        items = "".join(
            f'<li style="margin:4px 0;color:#b45309;font-size:14px;line-height:1.5;">'
            f'<span style="color:#b45309;font-weight:700;">⚠</span> {_esc(c)}</li>'
            for c in s.concerns
        )
        concerns_html = (
            f'<ul style="list-style:none;padding:0;margin:8px 0 0;">{items}</ul>'
        )

    url = _esc(j.url)
    return f"""
    <div style="background:#ffffff;border:1px solid #e5e5e7;border-radius:10px;padding:18px 20px;margin:0 0 16px;">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
        <tr>
          <td style="vertical-align:top;">
            <div style="font-size:12px;font-weight:600;color:#666;text-transform:uppercase;letter-spacing:0.4px;">{_esc(j.company)}</div>
            <div style="margin:4px 0 0;font-size:17px;font-weight:700;line-height:1.3;">
              <a href="{url}" style="color:#1d4ed8;text-decoration:none;">{_esc(j.title)}</a>
            </div>
          </td>
          <td style="vertical-align:top;width:60px;text-align:right;">
            <span style="display:inline-block;background:#1d4ed8;color:#ffffff;font-size:12px;font-weight:700;padding:5px 11px;border-radius:99px;white-space:nowrap;">{s.score}</span>
          </td>
        </tr>
      </table>
      <div style="margin-top:10px;font-size:12px;color:#555;">
        {_esc(j.location or '—')} · {_esc(j.source)} · Posted {_esc(_recency_label(j.posted_at))}{fresh_badge}{remote_badge}
      </div>
      <p style="margin:12px 0 0;font-size:14px;line-height:1.55;color:#333;">{_esc(s.fit_summary)}</p>
      {reasons_html}
      {concerns_html}
      <div style="margin-top:14px;">
        <a href="{url}" style="display:inline-block;background:#1d4ed8;color:#ffffff;font-size:13px;font-weight:600;padding:8px 14px;border-radius:6px;text-decoration:none;">View posting →</a>
      </div>
    </div>"""


def build_digest_html(scored: list[ScoredJob], slot: str) -> str:
    """Render the digest email as static HTML. Pure Python — no LLM call,
    no truncation risk, deterministic output."""
    label = _SLOT_LABELS.get(slot, slot.title())
    n = len(scored)
    plural = "s" if n != 1 else ""
    cards = "\n".join(_render_card(s) for s in scored)
    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>Job picks — {label}</title></head>
<body style="margin:0;padding:0;background:#f7f7f8;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#1a1a1a;">
  <div style="max-width:640px;margin:0 auto;padding:24px 16px;">
    <h1 style="font-size:22px;margin:0 0 6px;font-weight:700;">Your {label} picks</h1>
    <p style="color:#666;font-size:13px;margin:0 0 28px;">{n} role{plural} · sorted by fit score · agent-curated</p>
    {cards}
    <p style="color:#999;font-size:12px;text-align:center;margin:32px 0 0;line-height:1.5;">
      Sent by your personal job-watcher agent · {n} role{plural} this run<br>
      Tune in <code>profile/preferences.yaml</code> or raise the score threshold to narrow the digest.
    </p>
  </div>
</body>
</html>"""
