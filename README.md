# job-agent

A daily job-watcher for an MBA candidate. It:

1. Loads your resume + LinkedIn JSON + preferences as a static profile.
2. Pulls fresh postings from **LinkedIn, Indeed, Glassdoor, and Google Jobs** (via [python-jobspy](https://github.com/Bunsly/JobSpy)), plus direct ATS feeds from a curated list of 50 MBA-target companies (Greenhouse / Lever / Ashby).
3. Scores each posting against your profile using Claude (Haiku 4.5 for cheap pre-filter, Sonnet 4.6 for deep score). Prioritizes fresh postings.
4. Emails you a digest of the top matches.
5. Persists a SQLite dedup DB so you never see the same role twice.

Designed to run on GitHub Actions cron 2×/day (08:00 and 17:00 PDT).

## Sources

| Source | Type | Coverage |
|---|---|---|
| LinkedIn (via jobspy) | aggregator | Broad: any LinkedIn-posted role matching your query |
| Indeed (via jobspy) | aggregator | Broad: any Indeed-posted role |
| Glassdoor (via jobspy) | aggregator | Broad |
| Google Jobs (via jobspy) | aggregator | Broad — surfaces roles only listed on company sites |
| Greenhouse | ATS-direct | Per-company, from `companies.yaml` |
| Lever | ATS-direct | Per-company, from `companies.yaml` |
| Ashby | ATS-direct | Per-company, from `companies.yaml` |

The aggregator searches are driven by `search_queries` in [profile/preferences.yaml](profile/preferences.yaml). The ATS sources are a curated priority bonus — not a whitelist — and skip silently if a company slug 404s.

## Tradeoffs you should know

- **python-jobspy scrapes LinkedIn/Indeed.** It can break when those sites update their layout, and is technically against LinkedIn's ToS. Widely used for personal job-search; don't deploy at scale.
- **GitHub Actions runner IPs** are sometimes rate-limited by LinkedIn. If aggregator runs degrade over time, consider switching the workflow to run on a self-hosted runner or your Mac via launchd.
- **Recency window is 72 hours by default** (`recency_hours: 72`). Widen in [preferences.yaml](profile/preferences.yaml) if the digest comes back empty too often.

## Quick start (local)

```bash
git clone <your-fork>
cd job-agent
python -m venv .venv && source .venv/bin/activate
pip install -e .

cp .env.example .env   # then fill in API keys

# Fill in profile
$EDITOR profile/resume.md         # paste your resume as Markdown
$EDITOR profile/linkedin.json     # optional, but helpful
$EDITOR profile/preferences.yaml  # confirm functions, queries, location
$EDITOR companies.yaml            # prune/extend the 50-company seed list

# Dry run (no email sent; writes data/last_digest.html instead)
python -m src.main --dry-run --limit 30

# Real run
python -m src.main
```

## GitHub Actions setup

1. Push this repo to GitHub.
2. Repo Settings → Secrets and variables → Actions → add:
   - `ANTHROPIC_API_KEY`
   - `RESEND_API_KEY`
   - `EMAIL_TO`
   - `EMAIL_FROM` (e.g. `onboarding@resend.dev` until you verify a domain in Resend)
3. Repo Settings → Actions → General → Workflow permissions → **Read and write permissions** (so the workflow can commit `seen_jobs.db` back).
4. The workflow runs automatically at 08:00 and 17:00 PDT (cron in UTC). Adjust in [.github/workflows/run.yml](.github/workflows/run.yml).
5. Trigger manually: Actions tab → "job-watcher" → "Run workflow" (option to dry-run).

## Architecture

```
profile + preferences + companies
                │
                ▼
        ┌────────────────┐
        │ JobSpy source  │  LinkedIn / Indeed / Glassdoor / Google
        │ (search-driven)│
        ├────────────────┤
        │ ATS sources    │  Greenhouse / Lever / Ashby (per-company)
        └───────┬────────┘
                │
                ▼
         SQLite (dedup, INSERT OR IGNORE)
                │
                ▼
    stage-1 filter (Haiku 4.5)   ──▶ drop obvious mismatches
                │
                ▼
    stage-2 scorer (Sonnet 4.6)  ──▶ 0–100 score + reasons + concerns
                │                     recency-weighted
                ▼
    digest composer (Sonnet 4.6) ──▶ HTML email (recency surfaced)
                │
                ▼
              Resend
```

The candidate profile is passed as a **prompt-cached** system block, so per-job scoring stays cheap.

## Cost

≈ $0.30 per run, ~$18/month at 2 runs/day. Roughly half once cache hits stabilize.

## Files of interest

- [src/main.py](src/main.py) — orchestrator
- [src/scorer.py](src/scorer.py) — Claude calls (filter + score + digest)
- [src/sources/jobspy_source.py](src/sources/jobspy_source.py) — LinkedIn/Indeed/Glassdoor/Google aggregator
- [src/sources/greenhouse.py](src/sources/greenhouse.py), [lever.py](src/sources/lever.py), [ashby.py](src/sources/ashby.py) — ATS fetchers
- [src/prompts/](src/prompts/) — Claude prompts (tune these without touching code)
- [.github/workflows/run.yml](.github/workflows/run.yml) — cron + DB commit-back

## Tuning playbook

If digests are too **noisy**: narrow `search_queries`, tighten `target_functions`, raise `--threshold` (default 70).
If digests are too **sparse**: widen `recency_hours`, broaden queries, lower threshold.
If you keep seeing **the same companies**: add to `exclusions`.
If the scorer keeps **misjudging seniority**: edit [src/prompts/score.txt](src/prompts/score.txt) with concrete examples.
