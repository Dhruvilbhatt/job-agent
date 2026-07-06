from __future__ import annotations

import logging
import re

from .sources.base import JobPosting

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Free, deterministic title pre-filter.
#
# Aggregator queries ("Senior Consultant", "Product Manager", ...) drag in large
# volumes of off-target titles (IC engineering, Director/VP, internships, entry
# sales). Rejecting those here — before any LLM call — is the cheapest signal we
# have and directly shrinks the paid funnel.
#
# Design rule: HIGH PRECISION over recall. We only drop when the *title* clearly
# violates the candidate's stated exclusions. Anything ambiguous is kept and left
# to the (cheap) Haiku stage. This guarantees we never silently drop a relevant
# role on a fuzzy title.
# ---------------------------------------------------------------------------

# Titles that clearly violate the MBA candidate's stated direction (see
# profile/preferences.yaml `exclusions`). Ordered roughly by hit frequency.
_EXCLUSION_TITLE_PATTERNS: list[str] = [
    # Internships / early-career (candidate is graduating, targeting full-time)
    r"\bintern(ship)?\b",
    r"\bco-?op\b",
    r"\bnew\s+grad(uate)?\b",
    r"\bapprentice(ship)?\b",
    # Too senior (Director / VP / Head-of / C-suite) — "Chief of Staff" is allowed.
    r"\bdirector\b",
    r"\b(vice\s+president|vp)\b",
    r"\bhead\s+of\b",
    r"\bchief\b(?!\s+of\s+staff)",
    r"\b(svp|evp|cxo|ceo|cfo|coo|cto|cmo|cpo)\b",
    # IC engineering / software / hard-technical roles
    r"\b(software|backend|back-end|frontend|front-end|full[-\s]?stack|platform|"
    r"systems?|embedded|firmware|hardware|data|ml|machine\s+learning|ai|devops|"
    r"site\s+reliability|sre|security|network|cloud|qa|test|automation|mobile|"
    r"ios|android|web|game|gameplay|graphics)\s+engineer\b",
    r"\bengineer(ing)?\s+(i{1,3}|iv|v|[1-5])\b",
    r"\b(sr\.?|senior|staff|principal|lead)\s+engineer\b",
    r"\bswe\b",
    r"\b(software|application)\s+developer\b",
    r"\bprogrammer\b",
    r"\b(data|research)\s+scientist\b",
    r"\bdata\s+analyst\b",
    # Entry-level / quota-carrying sales
    r"\baccount\s+executive\b",
    r"\b(sdr|bdr)\b",
    r"\bsales\s+development\b",
    r"\bbusiness\s+development\s+representative\b",
    r"\bsales\s+representative\b",
    r"\binside\s+sales\b",
    # Clearly out-of-domain / non-professional titles that leak from aggregators
    r"\bregistered\s+nurse\b",
    r"\bwarehouse\b",
    r"\b(truck\s+)?driver\b",
    r"\bcashier\b",
    r"\bteller\b",
    r"\bcustodian\b",
    r"\btechnician\b",
]

_EXCLUSION_RE = re.compile("|".join(_EXCLUSION_TITLE_PATTERNS), re.IGNORECASE)


# ---------------------------------------------------------------------------
# Free experience-requirement filter.
#
# Drops postings whose description states a *clear minimum* years-of-experience
# above the candidate's ceiling (she's a new-grad MBA). Precision-first: we only
# fire on an unambiguous "<N> years ... experience" gate. Fuzzy seniority is left
# to the LLM (score.txt already penalizes it), so we never silently drop a role
# on a shaky signal.
#
# Recall guards:
#   * The number must sit right next to the word "experience" (so "4-year
#     degree" or "over the past 10 years ..." don't match).
#   * On a range ("5-7 years") we take the LOWER bound.
#   * We take the MINIMUM across all mentions, so "8+ yrs in X OR 2+ yrs in Y"
#     is kept (its true floor is 2).
# ---------------------------------------------------------------------------

# "<N>[+] [to/-/–] [<M>] year(s)/yr(s) ... experience"  (number precedes "experience")
_EXP_FORWARD_RE = re.compile(
    r"(\d{1,2})\s*(?:\+|plus)?\s*(?:[-–—]|to)?\s*(\d{1,2})?\s*\+?\s*"
    r"(?:years?|yrs?)[^.\n]{0,40}?experien",
    re.IGNORECASE,
)
# "experience of/with [at least] <N>[+] year(s)"  (explicit lead-in, avoids
# matching narrative like "experience over the past 10 years")
_EXP_REVERSE_RE = re.compile(
    r"experien\w*\s*(?:of|:|with|,)?\s*(?:at\s+least\s+|a\s+minimum\s+of\s+|min(?:imum)?\s+of\s+)?"
    r"(\d{1,2})\s*(?:\+|plus)?\s*(?:[-–—]|to)?\s*(\d{1,2})?\s*\+?\s*(?:years?|yrs?)",
    re.IGNORECASE,
)


def min_required_experience(text: str) -> int | None:
    """Return the lowest clearly-stated years-of-experience requirement in the
    text, or None if none is found. Deterministic and free."""
    if not text:
        return None
    floors: list[int] = []
    for rx in (_EXP_FORWARD_RE, _EXP_REVERSE_RE):
        for m in rx.finditer(text):
            try:
                floors.append(int(m.group(1)))  # lower bound of any range
            except (TypeError, ValueError):
                continue
    return min(floors) if floors else None


def drop_over_experienced(
    jobs: list[JobPosting], max_years: int = 4
) -> tuple[list[JobPosting], int]:
    """Drop postings whose stated minimum experience exceeds ``max_years``.

    Postings with no parseable experience requirement are KEPT (benefit of the
    doubt — the LLM still judges seniority). Returns (kept_jobs, dropped_count).
    """
    kept: list[JobPosting] = []
    dropped = 0
    for j in jobs:
        floor = min_required_experience(f"{j.title}\n{j.description}")
        if floor is not None and floor > max_years:
            log.debug("dropped (needs %d+ yrs exp): %s @ %s", floor, j.title, j.company)
            dropped += 1
        else:
            kept.append(j)
    return kept, dropped


def title_exclusion_reason(title: str) -> str | None:
    """Return the offending phrase if the title clearly violates an exclusion,
    else None. Purely deterministic and free."""
    if not title:
        return None
    m = _EXCLUSION_RE.search(title)
    return m.group(0) if m else None


def drop_by_title(jobs: list[JobPosting]) -> tuple[list[JobPosting], int]:
    """Drop postings whose *title* clearly violates a stated exclusion.

    High-precision by design — ambiguous titles are kept for the LLM stages.
    Returns (kept_jobs, dropped_count). Logs each drop at DEBUG for auditing.
    """
    kept: list[JobPosting] = []
    dropped = 0
    for j in jobs:
        reason = title_exclusion_reason(j.title)
        if reason:
            log.debug("dropped (title %r): %s @ %s", reason, j.title, j.company)
            dropped += 1
        else:
            kept.append(j)
    return kept, dropped
