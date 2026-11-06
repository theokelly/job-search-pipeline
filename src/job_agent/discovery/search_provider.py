"""
Search provider for fan-out discovery: Brave Search API.

Setup (one-time):
  1. Sign in at https://api-dashboard.search.brave.com (the account is the
     one created for this project; email + login live outside this repo).
  2. Confirm a Search plan subscription is active on the account.
  3. API Keys -> create a key for this pipeline.
  4. Add to .env in the repo root (passed into the container via
     docker-compose env_file, same as ANTHROPIC_API_KEY):
       BRAVE_API_KEY=<your key>
     BRAVE_SEARCH_API_KEY is accepted as a fallback name.

The provider surface is deliberately tiny — search(query, num) returning
[{title, url, snippet, page_age}] — so another provider can replace Brave
without touching fanout.py. page_age is Brave's crawl timestamp for the
result (ISO string or None); fanout uses it to order fetches freshest-first. Auth uses the X-Subscription-Token header; the key is
never logged.
"""
import os

import httpx


ENDPOINT = "https://api.search.brave.com/res/v1/web/search"


def _api_key() -> str:
    key = os.environ.get("BRAVE_API_KEY") or os.environ.get("BRAVE_SEARCH_API_KEY") or ""
    if not key:
        raise EnvironmentError(
            "Brave Search API not configured. Set BRAVE_API_KEY in "
            ".env (repo root). Create a key at "
            "https://api-dashboard.search.brave.com (API Keys section)."
        )
    return key


def search(query: str, num: int = 10) -> list[dict]:
    """Run one Brave web search. Returns [{title, url, snippet, page_age}, ...].

    `num` maps to Brave's `count` parameter (1-20 per request).
    Raises EnvironmentError when no API key is configured, matching how
    email_alerts signals missing Gmail config to run_discover.
    """
    resp = httpx.get(
        ENDPOINT,
        headers={
            "Accept": "application/json",
            "X-Subscription-Token": _api_key(),
        },
        params={"q": query, "count": max(1, min(int(num), 20))},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    results = ((data.get("web") or {}).get("results")) or []
    return [
        {
            "title": r.get("title", "") or "",
            "url": r.get("url", "") or "",
            "snippet": r.get("description", "") or "",
            "page_age": r.get("page_age"),
        }
        for r in results
    ]
