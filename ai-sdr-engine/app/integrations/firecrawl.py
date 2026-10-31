# app/integrations/firecrawl.py - Minimal Firecrawl v1 REST client (search + scrape) with call logging and LinkedIn exclusion
from __future__ import annotations

import os
import re
import asyncio
import time
from typing import Any, Optional
from urllib.parse import urlsplit

import httpx

from app import config  # noqa: F401 - importing config loads the repo .env into the environment
from app.llm import calllog

BASE_URL = "https://api.firecrawl.dev/v1"
TIMEOUT_S = 75.0
BLOCKED_HOSTS = ("linkedin.com",)


class FirecrawlError(RuntimeError):
    """Raised when the public web research service cannot be reached or rejects a request."""


# API key from the environment (FIRECRAWL_API_KEY), empty when not configured
def api_key() -> str:
    return os.environ.get("FIRECRAWL_API_KEY", "").strip()


# True when public web research is configured
def available() -> bool:
    return bool(api_key())


# True when a URL points at a host we must never fetch (LinkedIn and its subdomains)
def blocked(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in BLOCKED_HOSTS)


# Make markdown easier to quote: drop images, keep link text, collapse blank lines
def clean_markdown(text: str) -> str:
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text or "")
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"\\([\\`*_{}\[\]()#+\-.!|])", r"\1", text)
    text = re.sub(r"[\ue000-\uf8ff]\s?", "", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


_limit = asyncio.Semaphore(4)


# POST one request to Firecrawl (max 4 in flight, waits and retries on rate limits) and record it in the AI call log
async def _post(path: str, payload: dict[str, Any], purpose: str, kind: str) -> dict[str, Any]:
    if not available():
        raise FirecrawlError("not_configured")
    started = time.perf_counter()
    try:
        async with _limit:
            for attempt in range(4):
                async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
                    resp = await client.post(f"{BASE_URL}{path}", json=payload,
                                             headers={"Authorization": f"Bearer {api_key()}", "Content-Type": "application/json"})
                data = resp.json() if resp.content else {}
                if resp.status_code != 429 or attempt == 3:
                    break
                await asyncio.sleep(min(30.0, float(data.get("retry_after_seconds") or 5) + 1))
        if resp.status_code >= 400 or not data.get("success", False):
            raise FirecrawlError(f"http_{resp.status_code}: {str(data.get('error') or '')[:150]}")
    except Exception as exc:
        calllog.record(purpose, "firecrawl", kind, started, ok=False, usage=None, error=f"{type(exc).__name__}: {exc}"[:200])
        raise FirecrawlError(str(exc)[:200]) from exc
    calllog.record(purpose, "firecrawl", kind, started, ok=True, usage=None)
    return data


# Normalise one result record into {url,title,description,markdown}
def _page(url: str, title: Any, description: Any, markdown: Any) -> dict[str, str]:
    return {"url": str(url).split("#", 1)[0], "title": str(title or "").strip(), "description": str(description or "").strip(),
            "markdown": clean_markdown(str(markdown or ""))}


# Web search; with scrape=True each result also carries the page markdown. LinkedIn results are dropped.
async def search(query: str, limit: int = 5, tbs: Optional[str] = None, scrape: bool = True,
                 purpose: str = "web_search") -> list[dict[str, str]]:
    payload: dict[str, Any] = {"query": query, "limit": limit}
    if tbs:
        payload["tbs"] = tbs
    if scrape:
        payload["scrapeOptions"] = {"formats": ["markdown"], "onlyMainContent": True}
    data = await _post("/search", payload, purpose, "search")
    out = []
    for r in data.get("data") or []:
        url = r.get("url") or ""
        if not url.startswith(("http://", "https://")) or blocked(url):
            continue
        out.append(_page(url, r.get("title"), r.get("description"), r.get("markdown")))
    return out


# Scrape one public page as markdown (never LinkedIn)
async def scrape(url: str, purpose: str = "web_scrape") -> Optional[dict[str, str]]:
    if blocked(url) or not url.startswith(("http://", "https://")):
        return None
    data = await _post("/scrape", {"url": url, "formats": ["markdown"], "onlyMainContent": True}, purpose, "scrape")
    d = data.get("data") or {}
    meta = d.get("metadata") or {}
    return _page(meta.get("url") or meta.get("sourceURL") or url, meta.get("title"), meta.get("description"), d.get("markdown"))
