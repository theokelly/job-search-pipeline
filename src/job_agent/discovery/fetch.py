"""Single-URL job extraction — the shared door for one posting.

Recognized ATS board URLs (Ashby, Greenhouse, Lever) are fetched
through the ATS's own API (structured fields, canonical location);
every other URL falls back to the manual.fetch_from_url HTML cascade.

Used by fan-out discovery (per search hit), `job-agent ingest`
(agent-driven adds), and the UI Add-Job flow. Moved out of fanout.py
in Phase D of the refactor program so the extraction core has one
home that isn't a discovery channel.
"""
import re
from typing import Optional
from urllib.parse import urlparse, urlunparse

import httpx

from ..db import Job
from .normalizer import normalize_fanout, normalize_greenhouse, normalize_lever


class PostingGone(Exception):
    """The ATS itself says this posting no longer exists (an Ashby board
    that no longer lists it). Counted as `gone`, like an API 404."""


_ASHBY_RE = re.compile(r"jobs\.ashbyhq\.com/([^/]+)/([0-9a-f-]{36})", re.I)
_GREENHOUSE_RE = re.compile(r"(?:job-boards|boards)\.greenhouse\.io/([^/]+)/jobs/(\d+)", re.I)
_LEVER_RE = re.compile(r"jobs\.lever\.co/([^/]+)/([0-9a-f-]{36})", re.I)


def parse_ats_url(url: str) -> Optional[tuple[str, str, str]]:
    """Recognize a board posting URL. Returns (kind, board_slug, posting_id)."""
    for kind, rx in (("ashby", _ASHBY_RE), ("greenhouse", _GREENHOUSE_RE), ("lever", _LEVER_RE)):
        m = rx.search(url or "")
        if m:
            return kind, m.group(1), m.group(2)
    return None


def normalize_url(url: str) -> str:
    """Canonical form of a posting URL for cross-source dedupe.

    Known ATS postings reduce to scheme://host/<board>/<posting-id> (this
    drops Ashby /application suffixes and every query string, e.g.
    ?workplaceType=Remote). Other URLs keep their path but lose the query
    string and fragment, which is where tracking parameters live.
    """
    parsed = parse_ats_url(url or "")
    if parsed:
        kind, board, posting_id = parsed
        host = {
            "ashby": "jobs.ashbyhq.com",
            "greenhouse": "job-boards.greenhouse.io",
            "lever": "jobs.lever.co",
        }[kind]
        path = f"/{board}/jobs/{posting_id}" if kind == "greenhouse" else f"/{board}/{posting_id}"
        return f"https://{host}{path}"
    parts = urlparse(url or "")
    path = parts.path.rstrip("/")
    return urlunparse(("https", parts.netloc.lower(), path, "", "", ""))


def _company_from_slug(slug: str) -> str:
    """Display name guess for an unlisted company, from its board slug."""
    return slug.replace("-", " ").replace("_", " ").title()


def _fetch_ats_job(kind: str, board: str, posting_id: str) -> Optional[Job]:
    """Fetch one posting through its ATS's own API.

    Raises PostingGone when the board is live but the posting is not on
    it (Ashby); Greenhouse/Lever signal the same state as an HTTP 404."""
    company = _company_from_slug(board)
    if kind == "ashby":
        from .ashby import fetch_jobs as ashby_fetch

        for job in ashby_fetch(company, board):
            if posting_id in job.url:
                return job
        raise PostingGone(f"ashby board '{board}' no longer lists {posting_id}")
    if kind == "greenhouse":
        url = f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{posting_id}"
        resp = httpx.get(url, params={"content": "true"}, timeout=15)
        resp.raise_for_status()
        return normalize_greenhouse(company, resp.json())
    if kind == "lever":
        url = f"https://api.lever.co/v0/postings/{board}/{posting_id}"
        resp = httpx.get(url, params={"mode": "json"}, timeout=15)
        resp.raise_for_status()
        return normalize_lever(company, resp.json())
    return None


def fetch_job_from_url(url: str) -> Optional[Job]:
    """Extract one posting from a search result URL.

    Recognized ATS URLs go through the ATS API (structured fields, canonical
    location); everything else falls back to the manual.fetch_from_url
    cascade. Returns None when extraction yields no usable title/description;
    raises PostingGone when a recognized ATS reports the posting is gone.
    """
    parsed = parse_ats_url(url)
    if parsed:
        return _fetch_ats_job(*parsed)
    from .manual import fetch_from_url

    extracted = fetch_from_url(url)
    if not extracted.get("title") or not extracted.get("description"):
        return None
    return normalize_fanout(extracted)

