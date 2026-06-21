from __future__ import annotations

import logging

import resend

from .config import Config

log = logging.getLogger(__name__)


def send_digest(cfg: Config, subject: str, html: str) -> None:
    if not cfg.resend_api_key:
        log.warning("RESEND_API_KEY not set — skipping send")
        return
    resend.api_key = cfg.resend_api_key
    result = resend.Emails.send(
        {
            "from": cfg.email_from,
            "to": [cfg.email_to],
            "subject": subject,
            "html": html,
        }
    )
    log.info("email sent: id=%s", result.get("id") if isinstance(result, dict) else result)
