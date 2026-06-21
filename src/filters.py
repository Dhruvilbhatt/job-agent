from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

from .sources.base import JobPosting

log = logging.getLogger(__name__)

# Patterns that indicate the employer explicitly will NOT sponsor work visas.
# Conservative on purpose: we want hard, unambiguous statements only — anything
# fuzzier is left to the Claude scorer (which also reads these signals).
NO_SPONSORSHIP_PATTERNS = [
    r"\bno\s+(?:visa\s+|work\s+)?sponsorship\b",
    r"\bwithout\s+(?:visa\s+|the\s+need\s+for\s+)?sponsorship\b",
    r"\bunable\s+to\s+(?:offer|provide)\s+(?:visa\s+)?sponsorship\b",
    r"\bunable\s+to\s+sponsor\b",
    r"\bdo\s+not\s+(?:offer|provide)\s+(?:visa\s+|work\s+)?sponsorship\b",
    r"\bdoes\s+not\s+(?:offer|provide)\s+(?:visa\s+|work\s+)?sponsorship\b",
    r"\bwill\s+not\s+(?:offer|provide|sponsor)\b[^.\n]{0,40}sponsorship\b",
    r"\bcannot\s+(?:offer|provide|sponsor)\b[^.\n]{0,40}\b(?:visa|sponsorship)\b",
    r"\bnot\s+able\s+to\s+sponsor\b",
    r"\bno\s+h-?1b\b",
    r"\bh-?1b\s+(?:visa\s+)?(?:transfers?\s+)?not\s+(?:available|accepted|considered)\b",
    r"\bus\s+citizens?\s+only\b",
    r"\bus\s+citizens?\s+or\s+permanent\s+residents?\s+only\b",
    r"\bus\s+citizenship\s+(?:is\s+)?required\b",
    r"\bmust\s+be\s+(?:a\s+)?us\s+citizen\b",
    r"\bmust\s+have\s+(?:permanent\s+)?us\s+work\s+authoriz(?:ation|ed)\b",
    r"\bauthoriz(?:ed|ation)\s+to\s+work\s+in\s+the\s+(?:united\s+states|us)\s+(?:on\s+a\s+permanent\s+basis|without\s+(?:visa\s+)?sponsorship|without\s+the\s+need\s+for\s+sponsorship)\b",
    r"\bpermanent\s+work\s+authoriz(?:ation|ed)\s+(?:in\s+the\s+us\s+)?(?:is\s+)?required\b",
    r"\bwork\s+authoriz(?:ation|ed)\s+(?:in\s+the\s+us\s+)?(?:that\s+)?(?:does\s+not\s+|will\s+not\s+)require\s+sponsorship\b",
    r"\bnot\s+(?:accepting|considering)\s+candidates\s+(?:that|who)\s+require\s+(?:visa\s+)?sponsorship\b",
    r"\bsecurity\s+clearance\s+required\b",  # Often gates citizenship in practice.
]

NO_SPONSORSHIP_RE = re.compile("|".join(NO_SPONSORSHIP_PATTERNS), re.IGNORECASE)


def requires_no_sponsorship(text: str) -> bool:
    """True iff the posting explicitly states it will not sponsor work authorization."""
    if not text:
        return False
    return bool(NO_SPONSORSHIP_RE.search(text))


def drop_stale(jobs: list[JobPosting], hours: int) -> tuple[list[JobPosting], int]:
    """Remove jobs older than `hours`. Jobs with unknown posted_at are KEPT
    (no signal == benefit of the doubt; the scorer can still penalize them).

    Returns (kept_jobs, dropped_count).
    """
    if hours <= 0:
        return jobs, 0
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    kept: list[JobPosting] = []
    dropped = 0
    for j in jobs:
        if j.posted_at is None:
            kept.append(j)
            continue
        ts = j.posted_at if j.posted_at.tzinfo else j.posted_at.replace(tzinfo=timezone.utc)
        if ts >= cutoff:
            kept.append(j)
        else:
            dropped += 1
    return kept, dropped


def drop_no_sponsorship(jobs: list[JobPosting]) -> tuple[list[JobPosting], int]:
    """Remove jobs whose title+description explicitly say no sponsorship.

    Returns (kept_jobs, dropped_count). Logs each drop at debug level so the
    user can audit (set LOG_LEVEL=DEBUG to see).
    """
    kept: list[JobPosting] = []
    dropped = 0
    for j in jobs:
        haystack = f"{j.title}\n{j.description}"
        if requires_no_sponsorship(haystack):
            log.debug("dropped (no sponsorship): %s @ %s", j.title, j.company)
            dropped += 1
        else:
            kept.append(j)
    return kept, dropped
