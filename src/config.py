from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Config:
    anthropic_api_key: str
    resend_api_key: str
    email_to: str
    email_from: str
    model_filter: str
    model_scorer: str
    model_digest: str

    @classmethod
    def from_env(cls) -> "Config":
        def req(k: str) -> str:
            v = os.environ.get(k)
            if not v:
                raise RuntimeError(f"Missing required env var: {k}")
            return v

        return cls(
            anthropic_api_key=req("ANTHROPIC_API_KEY"),
            resend_api_key=os.environ.get("RESEND_API_KEY", ""),
            email_to=req("EMAIL_TO"),
            email_from=os.environ.get("EMAIL_FROM", "onboarding@resend.dev"),
            model_filter=os.environ.get("MODEL_FILTER", "claude-haiku-4-5-20251001"),
            model_scorer=os.environ.get("MODEL_SCORER", "claude-sonnet-4-6"),
            model_digest=os.environ.get("MODEL_DIGEST", "claude-sonnet-4-6"),
        )
