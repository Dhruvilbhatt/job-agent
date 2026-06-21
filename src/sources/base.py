from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime

import httpx
from bs4 import BeautifulSoup


@dataclass
class JobPosting:
    source: str
    company: str
    title: str
    location: str
    url: str
    description: str
    remote: bool = False
    posted_at: datetime | None = None
    external_id: str = ""
    id: str = field(init=False)

    def __post_init__(self) -> None:
        raw = f"{self.source}:{self.external_id or self.url}"
        self.id = hashlib.sha256(raw.encode()).hexdigest()[:32]


class JobSource:
    name: str = "base"

    async def fetch(self, client: httpx.AsyncClient) -> list[JobPosting]:
        raise NotImplementedError


def strip_html(html: str, max_chars: int = 8000) -> str:
    if not html:
        return ""
    text = BeautifulSoup(html, "html.parser").get_text(separator="\n")
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text[:max_chars]
