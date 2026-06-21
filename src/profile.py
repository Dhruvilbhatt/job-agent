from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
PROFILE_DIR = ROOT / "profile"
COMPANIES_PATH = ROOT / "companies.yaml"


@dataclass
class Profile:
    resume_md: str
    linkedin: dict
    preferences: dict

    def as_prompt_block(self) -> str:
        parts = ["# Candidate Profile", "", "## Resume", "", self.resume_md.strip()]
        if self.linkedin:
            parts += ["", "## LinkedIn (structured)", "", "```json", json.dumps(self.linkedin, indent=2), "```"]
        if self.preferences:
            parts += ["", "## Stated Preferences", "", "```yaml", yaml.safe_dump(self.preferences, sort_keys=False).strip(), "```"]
        return "\n".join(parts)


def load_profile() -> Profile:
    resume = (PROFILE_DIR / "resume.md").read_text()

    linkedin_path = PROFILE_DIR / "linkedin.json"
    linkedin = json.loads(linkedin_path.read_text()) if linkedin_path.exists() else {}

    prefs_path = PROFILE_DIR / "preferences.yaml"
    prefs = yaml.safe_load(prefs_path.read_text()) if prefs_path.exists() else {}

    return Profile(resume_md=resume, linkedin=linkedin, preferences=prefs)


def load_companies() -> list[dict]:
    data = yaml.safe_load(COMPANIES_PATH.read_text())
    return data.get("companies", [])
