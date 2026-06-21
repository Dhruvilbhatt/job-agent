"""Google Sheets exporter for the job-agent.

Upserts every scored job into a single sheet. Subsequent runs refresh agent-owned
columns (score, summary, etc.) but never touch user-edited columns: `status` and
`notes`. If the Sheets API is misconfigured or unreachable, the exporter logs a
warning and returns — never blocks the rest of the run.

Auth: service account. Credentials sourced from one of:
  - GOOGLE_SHEETS_CREDS_JSON       (full JSON string — used in GitHub Actions)
  - GOOGLE_SHEETS_CREDS_JSON_PATH  (path to a JSON file — used locally)

Both env vars optional. If neither is set, or GOOGLE_SHEETS_SHEET_ID is missing,
the exporter no-ops silently.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

import gspread
from google.oauth2.service_account import Credentials

from .scorer import ScoredJob, _days_old, _recency_label

log = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# Column order is the contract: never reorder without a migration.
HEADERS = [
    "id",
    "first_seen",
    "last_seen",
    "posted",
    "emailed",
    "company",
    "title",
    "location",
    "remote",
    "source",
    "url",
    "score",
    "fit_summary",
    "match_reasons",
    "concerns",
    "status",     # USER-EDITABLE — never overwritten on subsequent runs
    "notes",      # USER-EDITABLE — never overwritten on subsequent runs
]
USER_EDITABLE = {"status", "notes"}


def _load_credentials() -> Credentials | None:
    raw = os.environ.get("GOOGLE_SHEETS_CREDS_JSON")
    if raw:
        try:
            info = json.loads(raw)
            return Credentials.from_service_account_info(info, scopes=SCOPES)
        except Exception as e:
            log.warning("failed to parse GOOGLE_SHEETS_CREDS_JSON: %s", e)
            return None

    path = os.environ.get("GOOGLE_SHEETS_CREDS_JSON_PATH")
    if path and os.path.exists(path):
        try:
            return Credentials.from_service_account_file(path, scopes=SCOPES)
        except Exception as e:
            log.warning("failed to load creds file %s: %s", path, e)
            return None

    return None


def _row_for(s: ScoredJob, now_iso: str, emailed_at: str | None) -> dict[str, str]:
    j = s.job
    return {
        "id": j.id,
        "first_seen": now_iso,
        "last_seen": now_iso,
        "posted": _recency_label(j.posted_at),
        "emailed": emailed_at or "",
        "company": j.company,
        "title": j.title,
        "location": j.location,
        "remote": "yes" if j.remote else "no",
        "source": j.source,
        "url": j.url,
        "score": str(s.score),
        "fit_summary": s.fit_summary,
        "match_reasons": " | ".join(s.match_reasons),
        "concerns": " | ".join(s.concerns),
        "status": "new",
        "notes": "",
    }


class GSheetExporter:
    def __init__(self) -> None:
        self.sheet_id = os.environ.get("GOOGLE_SHEETS_SHEET_ID", "").strip()
        self.tab_name = os.environ.get("GOOGLE_SHEETS_TAB", "jobs").strip() or "jobs"
        self._creds = _load_credentials()

    def enabled(self) -> bool:
        return bool(self.sheet_id and self._creds)

    def export(self, scored: list[ScoredJob], emailed_ids: set[str]) -> None:
        if not self.enabled():
            log.info("gsheet: not configured (set GOOGLE_SHEETS_SHEET_ID and creds); skipping")
            return
        if not scored:
            log.info("gsheet: no scored jobs this run; skipping")
            return

        try:
            client = gspread.authorize(self._creds)
            spreadsheet = client.open_by_key(self.sheet_id)
            ws = self._get_or_create_tab(spreadsheet)
            self._ensure_headers(ws)
            existing = self._read_existing(ws)
            self._upsert(ws, scored, existing, emailed_ids)
            log.info("gsheet: synced %d jobs to %s/%s", len(scored), self.sheet_id, self.tab_name)
        except Exception as e:
            log.warning("gsheet export failed (non-fatal): %s", e)

    def _get_or_create_tab(self, spreadsheet):
        try:
            return spreadsheet.worksheet(self.tab_name)
        except gspread.WorksheetNotFound:
            return spreadsheet.add_worksheet(title=self.tab_name, rows=2000, cols=len(HEADERS))

    def _ensure_headers(self, ws) -> None:
        first_row = ws.row_values(1) if ws.row_count else []
        if first_row != HEADERS:
            ws.update("A1", [HEADERS])
            ws.freeze(rows=1)

    def _read_existing(self, ws) -> dict[str, dict]:
        """Return {id: {col_name: value, _row: 1-indexed row number}}."""
        values = ws.get_all_values()
        if len(values) < 2:
            return {}
        out: dict[str, dict] = {}
        for i, row in enumerate(values[1:], start=2):
            padded = row + [""] * (len(HEADERS) - len(row))
            rec = dict(zip(HEADERS, padded))
            rec["_row"] = i
            jid = rec.get("id")
            if jid:
                out[jid] = rec
        return out

    def _upsert(
        self,
        ws,
        scored: list[ScoredJob],
        existing: dict[str, dict],
        emailed_ids: set[str],
    ) -> None:
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        emailed_at = now_iso  # any job in emailed_ids was emailed during this run

        updates: list[dict] = []
        appends: list[list[str]] = []

        for s in scored:
            new_row = _row_for(
                s,
                now_iso,
                emailed_at if s.job.id in emailed_ids else None,
            )

            prior = existing.get(s.job.id)
            if prior:
                # Preserve user-editable columns from the existing row.
                for col in USER_EDITABLE:
                    if prior.get(col):
                        new_row[col] = prior[col]
                # Preserve original first_seen.
                if prior.get("first_seen"):
                    new_row["first_seen"] = prior["first_seen"]
                # Don't clobber a prior emailed timestamp with empty.
                if prior.get("emailed") and not new_row["emailed"]:
                    new_row["emailed"] = prior["emailed"]
                row_num = prior["_row"]
                updates.append(
                    {
                        "range": f"A{row_num}:{_col_letter(len(HEADERS))}{row_num}",
                        "values": [[new_row[h] for h in HEADERS]],
                    }
                )
            else:
                appends.append([new_row[h] for h in HEADERS])

        if updates:
            ws.batch_update(updates, value_input_option="USER_ENTERED")
        if appends:
            ws.append_rows(appends, value_input_option="USER_ENTERED")


def _col_letter(n: int) -> str:
    """1->A, 26->Z, 27->AA. Spreadsheet column letters."""
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s
