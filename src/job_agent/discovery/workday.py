"""Workday discovery via the public CXS (candidate experience) JSON API.

Many large employers (Philips, Unilever, …) run Workday with no other
machine-readable board. The CXS API needs no auth:

  POST https://{host}/wday/cxs/{tenant}/{site}/jobs
  body: {"searchText": "", "appliedFacets": {}, "limit": 50, "offset": 0}

Job details (full description) come from:

  GET https://{host}/wday/cxs/{tenant}/{site}{externalPath}

`workday_board` in theo.yaml is "host/site", e.g.
"philips.wd3.myworkdayjobs.com/jobs-and-careers".
"""
import httpx
from ..db import Job
from .normalizer import normalize_workday


# Server-side keyword searches keep detail-fetch volume sane (Philips has
# 800+ postings; only a fraction are data/AI/strategy roles).
DEFAULT_QUERIES = [
    "analytics",
    "artificial intelligence",
    "data",
    "strategy",
    "transformation",
    "chief of staff",
]

_HEADERS = {
    "Content-Type": "application/json",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
}


def _split_board(workday_board: str) -> tuple[str, str, str]:
    """Split "host/site" into (host, tenant, site). Tenant is the first
    DNS label of the host (e.g. philips.wd3.myworkdayjobs.com -> philips)."""
    host, _, site = workday_board.partition("/")
    tenant = host.split(".")[0]
    return host, tenant, site


def _list_postings(host: str, tenant: str, site: str, search_text: str) -> list[dict]:
    url = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
    postings: list[dict] = []
    offset = 0
    # Page size 20: some tenants (e.g. Philips) reject larger limits with 400.
    limit = 20
    while True:
        resp = httpx.post(
            url,
            json={"searchText": search_text, "appliedFacets": {},
                  "limit": limit, "offset": offset},
            headers=_HEADERS,
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()
        batch = data.get("jobPostings", [])
        if not batch:
            break
        postings.extend(batch)
        offset += len(batch)
        if offset >= data.get("total", 0):
            break
    return postings


def _fetch_detail(host: str, tenant: str, site: str, external_path: str) -> dict:
    url = f"https://{host}/wday/cxs/{tenant}/{site}{external_path}"
    resp = httpx.get(url, headers=_HEADERS, timeout=20)
    resp.raise_for_status()
    return resp.json()


def fetch_jobs(
    company: str,
    workday_board: str,
    queries: list[str] | None = None,
    max_details: int = 150,
) -> list[Job]:
    """Fetch postings matching any of `queries` (union, deduped by path).

    Each query runs server-side; only matching postings get a detail fetch.
    `max_details` bounds HTTP volume per company.
    """
    host, tenant, site = _split_board(workday_board)
    queries = queries if queries is not None else DEFAULT_QUERIES

    seen: dict[str, dict] = {}  # externalPath -> posting
    for q in queries:
        for p in _list_postings(host, tenant, site, q):
            path = p.get("externalPath", "")
            if path:
                seen.setdefault(path, p)

    jobs: list[Job] = []
    for path, posting in list(seen.items())[:max_details]:
        try:
            detail = _fetch_detail(host, tenant, site, path)
        except Exception:
            detail = {}
        jobs.append(normalize_workday(company, host, site, posting, detail))
    return jobs
