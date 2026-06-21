# job-agent

A daily job-watcher for an MBA candidate. It:

1. Loads your resume + LinkedIn JSON + preferences as a static profile.
2. Pulls fresh postings from **LinkedIn, Indeed, Glassdoor, and Google Jobs** (via [python-jobspy](https://github.com/Bunsly/JobSpy)), plus direct ATS feeds from a curated list of 50 MBA-target companies (Greenhouse / Lever / Ashby).
3. Drops postings that explicitly require no sponsorship (regex + LLM safety net) and anything older than the recency window.
4. Scores each posting against your profile using Claude (Haiku 4.5 for cheap pre-filter, Sonnet 4.6 for deep score). Prioritizes fresh postings.
5. Emails you a digest of the top matches (rendered from a deterministic Python template — no LLM truncation risk).
6. Persists a SQLite dedup DB so you never see the same role twice.
7. **Optionally** upserts every scored job into a Google Sheet for tracking (`status` and `notes` columns are yours to edit; never overwritten on re-runs).

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
   - `GOOGLE_SHEETS_SHEET_ID` *(optional — only if you want sheet export)*
   - `GOOGLE_SHEETS_CREDS_JSON` *(optional — full contents of your service-account JSON)*
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
  stale + sponsorship filters    ──▶ regex pre-filters, free
                │
                ▼
       SQLite dedup (INSERT OR IGNORE)
                │
                ▼
   stage-1 filter (Haiku 4.5)    ──▶ drop obvious mismatches
                │
                ▼
   stage-2 scorer (Sonnet 4.6)   ──▶ 0–100 score + reasons + concerns
                │                      recency-weighted
                ├─────────────────────────────┐
                ▼                             ▼
   digest render (Python template)    Google Sheets upsert
                │                     (preserves `status` + `notes`)
                ▼
              Resend
```

The candidate profile is passed as a **prompt-cached** system block, so per-job scoring stays cheap. The digest is rendered from a deterministic Python template (no LLM call, no truncation risk).

## Cost

≈ $0.30 per run, ~$18/month at 2 runs/day. Roughly half once cache hits stabilize.

## Files of interest

- [src/main.py](src/main.py) — orchestrator
- [src/scorer.py](src/scorer.py) — Claude scoring + Python digest template
- [src/sources/jobspy_source.py](src/sources/jobspy_source.py) — LinkedIn/Indeed/Glassdoor/Google aggregator
- [src/sources/greenhouse.py](src/sources/greenhouse.py), [lever.py](src/sources/lever.py), [ashby.py](src/sources/ashby.py) — ATS fetchers
- [src/filters.py](src/filters.py) — sponsorship + recency pre-filters
- [src/gsheet.py](src/gsheet.py) — Google Sheets exporter
- [src/store.py](src/store.py) — SQLite dedup
- [src/prompts/](src/prompts/) — Claude prompts (tune these without touching code)
- [.github/workflows/run.yml](.github/workflows/run.yml) — cron + DB commit-back

## Google Sheets export (optional but recommended)

Upserts every scored job — not just emailed picks — into a single sheet so you have a searchable, editable history.

**Columns** (in order):

| Agent-owned (refreshed each run) | User-editable (preserved across runs) |
|---|---|
| `id`, `first_seen`, `last_seen`, `posted`, `emailed`, `company`, `title`, `location`, `remote`, `source`, `url`, `score`, `fit_summary`, `match_reasons`, `concerns` | `status`, `notes` |

Suggested `status` lifecycle: `new` → `interested` → `applied` → `interviewing` → `offer` / `passed` / `rejected`.

**Setup (one-time, ~10 min)**

1. **GCP project + service account** — https://console.cloud.google.com → create project → enable **Google Sheets API** and **Google Drive API** → IAM → Service Accounts → create `job-agent-bot` → Keys → Add Key → JSON. Download the JSON.
2. **Create the sheet** — https://sheets.new. Copy the **Sheet ID** from the URL (the chunk between `/d/` and `/edit`).
3. **Share with the bot** — open the JSON, find `client_email`. In the sheet → Share → paste that email → Editor.
4. **Wire up locally** — save the JSON as `gsheet-creds.json` in the repo root (it's gitignored). Add to `.env`:
   ```
   GOOGLE_SHEETS_SHEET_ID=<your-sheet-id>
   GOOGLE_SHEETS_CREDS_JSON_PATH=./gsheet-creds.json
   ```
5. **GitHub Actions** — add two repo secrets: `GOOGLE_SHEETS_SHEET_ID` (the ID) and `GOOGLE_SHEETS_CREDS_JSON` (the entire JSON contents pasted in).

**Behavior**

- Runs at the end of every job-agent run, after the email send.
- Failure is non-fatal — a Sheets API error logs a warning and the run continues. Email and dedup are not blocked.
- If env vars aren't configured, the exporter no-ops silently.
- On each run: existing rows have everything *except* `status` and `notes` refreshed; new rows are appended.
- Tab name defaults to `jobs`. Override with `GOOGLE_SHEETS_TAB`.

**Security**

- The JSON private key gives write access to any sheet shared with the bot. Treat it like a password.
- Rotate it (GCP → Service Account → Keys → Add new → delete old) if it ever leaves your machine.
- `gsheet-creds.json` and `*-service-account.json` are gitignored by default.

## Tuning playbook

If digests are too **noisy**: narrow `search_queries`, tighten `target_functions`, raise `--threshold` (default 70).
If digests are too **sparse**: widen `recency_hours`, broaden queries, lower threshold.
If you keep seeing **the same companies**: add to `exclusions`.
If the scorer keeps **misjudging seniority**: edit [src/prompts/score.txt](src/prompts/score.txt) with concrete examples.
