"""
LinkedIn discovery via the public guest jobs API.

LinkedIn exposes a guest-accessible search endpoint that returns job cards without
requiring authentication. This module uses it to discover jobs matching the target
search queries, then fetches full JDs from the individual job view pages.

Rate limiting: LinkedIn returns 429 if requests come too fast. A 2s inter-request
delay keeps things well within safe limits for personal use.
"""
import re
import time
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime
from typing import Optional

from bs4 import BeautifulSoup

from ..db import Job, job_id as make_job_id


_GUEST_SEARCH_URL = (
    "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
    "?keywords={keywords}&location={location}&f_TPR={timespan}&start={start}"
)
_GUEST_SEARCH_URL_WITH_COMPANY = (
    "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
    "?keywords={keywords}&location={location}&f_TPR={timespan}&f_C={company_id}&start={start}"
)
_JOB_VIEW_URL = "https://www.linkedin.com/jobs/view/{job_id}/"
# Guest fragment endpoint — this is where LinkedIn renders the closed-posting
# marker (a `figcaption.closed-job`) for unauthenticated clients.
_JOB_POSTING_URL = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# LinkedIn timespan filter values
TIMESPAN_DAY = "r86400"
TIMESPAN_WEEK = "r604800"
TIMESPAN_MONTH = "r2592000"


def _fetch(url: str, timeout: int = 6) -> tuple[Optional[int], Optional[str]]:
    """Fetch `url` and return (status_code, body_text).

    status_code is the HTTP status (e.g. 200, 404, 429); None means the request
    raised before getting a response (timeout, DNS, connection reset). body_text
    is the decoded response body, or None when there was none to read. Keeping the
    status lets callers tell "posting gone" (404) apart from "fetch failed / rate
    limited" (None/429) instead of collapsing everything to None.
    """
    try:
        req = urllib.request.Request(url, headers=_HEADERS)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            body = None
        return e.code, body
    except Exception:
        return None, None


def _get(url: str, timeout: int = 6) -> Optional[BeautifulSoup]:
    status, text = _fetch(url, timeout=timeout)
    if status == 200 and text:
        return BeautifulSoup(text, "html.parser")
    return None


def _fetch_job_page(job_id: str) -> tuple[str, dict]:
    """Fetch a LinkedIn job view page and return (description, metadata).

    metadata keys: title, company, location (any may be missing if page blocked).
    """
    soup = _get(_JOB_VIEW_URL.format(job_id=job_id))
    if not soup:
        return "", {}

    # Description
    description = ""
    div = soup.find("div", class_="description__text description__text--rich")
    if div:
        for el in div.find_all(["span", "a"]):
            el.decompose()
        description = div.get_text(separator="\n").strip()[:8000]

    # Metadata — LinkedIn uses several class names across page versions
    meta: dict = {}
    title_el = (
        soup.find("h1", class_=lambda c: c and "top-card-layout__title" in c)
        or soup.find("h2", class_=lambda c: c and "top-card-layout__title" in c)
        or soup.find("h1", class_=lambda c: c and "topcard__title" in c)
    )
    company_el = (
        soup.find("a", class_=lambda c: c and "topcard__org-name-link" in c)
        or soup.find("span", class_=lambda c: c and "topcard__org-name-link" in c)
        or soup.find("a", class_=lambda c: c and "top-card-layout__company-url" in c)
    )
    location_el = (
        soup.find("span", class_=lambda c: c and "topcard__flavor--bullet" in c)
        or soup.find("span", class_=lambda c: c and "top-card-layout__first-subline" in c)
    )

    if title_el:
        meta["title"] = title_el.get_text(" ", strip=True)
    if company_el:
        meta["company"] = company_el.get_text(" ", strip=True)
    if location_el:
        meta["location"] = location_el.get_text(" ", strip=True)

    return description, meta


def _fetch_description(job_id: str) -> str:
    """Fetch just the description (backward-compat shim)."""
    desc, _ = _fetch_job_page(job_id)
    return desc


# ---------------------------------------------------------------------------
# Closed-posting detection
# ---------------------------------------------------------------------------

# Phrases LinkedIn shows once a posting stops taking applications. Matched
# case-insensitively against the page's visible text so detection doesn't depend
# on LinkedIn's obfuscated, frequently-rotated CSS class names (e.g. the
# `_37d1e1ab …` wrapper around the "No longer accepting applications" warning).
_CLOSED_MARKERS = (
    "no longer accepting applications",
    "no longer accepting application",
    "this job is no longer available",
    "this job posting is no longer available",
    "this job has expired",
)


def is_linkedin_url(url: Optional[str]) -> bool:
    return bool(url) and "linkedin.com" in url


def _extract_job_id(url: Optional[str]) -> Optional[str]:
    """Pull the numeric posting id out of a LinkedIn job URL."""
    if not url:
        return None
    m = re.search(r"/jobs/view/(\d+)", url)
    if m:
        return m.group(1)
    # Slug-style or query-param URLs: .../view/some-title-1234567890 or ?currentJobId=…
    m = re.search(r"(?:currentJobId=|-)(\d{6,})", url)
    return m.group(1) if m else None


def _body_shows_closed(html: Optional[str]) -> bool:
    """True if raw page HTML shows a 'no longer accepting' warning.

    Cheap substring scan over the raw body — avoids building a BeautifulSoup tree
    and running get_text() over the whole document for every posting checked
    (hundreds per run). Matches both LinkedIn's guest `closed-job` marker class
    and the known human-readable phrases, case-insensitively.
    """
    if not html:
        return False
    low = html.lower()
    if "closed-job" in low:
        return True
    return any(marker in low for marker in _CLOSED_MARKERS)


def _page_shows_closed(soup: Optional[BeautifulSoup]) -> bool:
    """True if a fetched LinkedIn job page shows a 'no longer accepting' warning.

    Checks LinkedIn's guest `closed-job` figcaption first, then falls back to a
    generalized case-insensitive text search for known closed-posting phrases so
    it keeps working across page variants and markup changes.
    """
    if soup is None:
        return False
    if soup.find(class_=lambda c: c and "closed-job" in c):
        return True
    text = soup.get_text(" ", strip=True).lower()
    return any(marker in text for marker in _CLOSED_MARKERS)


def is_posting_closed(url: Optional[str], request_delay: float = 0.0) -> Optional[bool]:
    """Return whether the LinkedIn posting at `url` is closed to applications.

    True  -> page shows a 'No longer accepting applications' style warning
    False -> page fetched and shows no such warning
    None  -> not a LinkedIn URL, or the page couldn't be fetched (unknown; caller
             should NOT prune on None so transient fetch failures don't drop jobs)
    """
    if not is_linkedin_url(url):
        return None
    job_id = _extract_job_id(url)
    if not job_id:
        return None
    # The guest jobPosting fragment is where the closed marker renders for
    # unauthenticated clients; fall back to the full job view page when the
    # fragment doesn't come back cleanly (404/429/transient error).
    status, text = _fetch(_JOB_POSTING_URL.format(job_id=job_id))
    if status != 200 or not text:
        status, text = _fetch(_JOB_VIEW_URL.format(job_id=job_id))
    if request_delay:
        time.sleep(request_delay)
    if status != 200 or not text:
        # Couldn't fetch either endpoint — unknown. Caller must NOT prune on
        # None so transient failures / rate limiting don't drop live jobs.
        return None
    return _body_shows_closed(text)


def _parse_cards(soup: BeautifulSoup) -> list[dict]:
    """Extract job card metadata from a search results page."""
    jobs = []
    for card in soup.find_all("div", class_="base-search-card__info"):
        title_el = card.find("h3")
        company_el = card.find("a", class_="hidden-nested-link")
        location_el = card.find("span", class_="job-search-card__location")
        parent = card.parent
        urn = parent.get("data-entity-urn", "")
        job_id = urn.split(":")[-1] if urn else ""
        if not job_id:
            continue
        jobs.append({
            "job_id": job_id,
            "title": title_el.text.strip() if title_el else "",
            "company": company_el.text.strip().replace("\n", " ") if company_el else "",
            "location": location_el.text.strip() if location_el else "",
        })
    return jobs


_BAD_TITLES = {
    "", "job description", "about us", "about us:", "about the role", "about the company",
    "the company", "financial services / fintech", "sazerac company overview",
}


def backfill_missing_jds(session, request_delay: float = 2.0) -> int:
    """Fetch JDs (and fix bad metadata) for linkedin jobs that are missing data.

    Fixes two cases:
    1. description_raw is empty
    2. company is "Unknown"/blank or title is clearly body text (in _BAD_TITLES)
    """
    import re
    from sqlmodel import select
    from ..db import Job

    jobs = session.exec(
        select(Job).where(Job.source.in_(["linkedin", "linkedin_search"]))
    ).all()

    def _needs_fix(j: Job) -> bool:
        if not (j.description_raw or "").strip():
            return True
        company_bad = (j.company or "").strip().lower() in ("unknown", "")
        title_bad = (j.title or "").strip().lower() in _BAD_TITLES
        return company_bad or title_bad

    to_fix = [j for j in jobs if _needs_fix(j)]

    updated = 0
    for job in to_fix:
        if not job.url:
            continue
        m = re.search(r"/jobs/view/(\d+)", job.url)
        if not m:
            continue
        description, meta = _fetch_job_page(m.group(1))
        changed = False
        if description and not (job.description_raw or "").strip():
            job.description_raw = description
            changed = True
        if meta.get("title") and (not job.title or job.title.lower() in _BAD_TITLES or len(job.title) > 100):
            job.title = meta["title"]
            changed = True
        if meta.get("company") and (not job.company or job.company.lower() in ("unknown", "")):
            job.company = meta["company"]
            changed = True
        if meta.get("location") and not job.location:
            job.location = meta["location"]
            changed = True
        if changed:
            updated += 1
        time.sleep(request_delay / 2)

    return updated


def fetch_jobs(
    search_queries: list[dict],
    pages_per_query: int = 2,
    timespan: str = TIMESPAN_WEEK,
    request_delay: float = 2.0,
    known_ids: set[str] | None = None,
    refetch_jds: bool = False,
) -> list[Job]:
    """
    Discover LinkedIn jobs via the guest search API.

    search_queries: list of {"keywords": str, "location": str}
    pages_per_query: number of result pages to fetch (25 jobs per page)
    timespan: one of TIMESPAN_DAY, TIMESPAN_WEEK, TIMESPAN_MONTH
    request_delay: seconds to wait between requests to avoid 429s
    known_ids: set of DB job IDs already in the DB with descriptions — JD fetch skipped for these
    refetch_jds: when True, ignore known_ids and re-fetch every JD (useful when
        prior LinkedIn fetches were imperfect and a clean re-pull is needed)

    Returns Job records with description_raw populated.
    """
    now = datetime.utcnow()
    all_cards: list[dict] = []

    total_queries = len(search_queries)
    for qi, query in enumerate(search_queries, 1):
        keywords = urllib.parse.quote(query["keywords"])
        location = urllib.parse.quote(query.get("location", ""))
        company_id = query.get("company_id")
        label = query["keywords"]
        if company_id:
            label += f" @company:{company_id}"
        elif query.get("location"):
            label += f" @ {query['location']}"
        print(f"  [linkedin] query {qi}/{total_queries}: {label}", flush=True)
        for page in range(pages_per_query):
            if company_id:
                url = _GUEST_SEARCH_URL_WITH_COMPANY.format(
                    keywords=keywords,
                    location=location,
                    timespan=timespan,
                    company_id=company_id,
                    start=page * 25,
                )
            else:
                url = _GUEST_SEARCH_URL.format(
                    keywords=keywords,
                    location=location,
                    timespan=timespan,
                    start=page * 25,
                )
            soup = _get(url)
            if soup:
                cards = _parse_cards(soup)
                all_cards.extend(cards)
            time.sleep(request_delay / 2)

    # Deduplicate by job_id
    seen: set[str] = set()
    unique_cards = []
    for c in all_cards:
        if c["job_id"] not in seen:
            seen.add(c["job_id"])
            unique_cards.append(c)

    jobs: list[Job] = []
    needs_jd = []
    skipped_jd = 0
    for card in unique_cards:
        db_id = make_job_id(card["company"], card["title"], card["location"])
        if known_ids and db_id in known_ids and not refetch_jds:
            skipped_jd += 1
            jobs.append(Job(
                id=db_id,
                source="linkedin_search",
                company=card["company"],
                title=card["title"],
                location=card["location"],
                url=_JOB_VIEW_URL.format(job_id=card["job_id"]),
                description_raw="",  # upsert_job will keep existing description
                first_seen=now,
                last_seen=now,
                status="active",
            ))
        else:
            needs_jd.append((db_id, card))

    if skipped_jd:
        print(f"  [linkedin] skipped JD fetch for {skipped_jd} already-known jobs", flush=True)

    for i, (db_id, card) in enumerate(needs_jd, 1):
        description = _fetch_description(card["job_id"])
        if i % 10 == 0 or i == len(needs_jd):
            print(f"  [linkedin] fetching JDs: {i}/{len(needs_jd)}", flush=True)
        time.sleep(request_delay / 2)

        jobs.append(Job(
            id=make_job_id(card["company"], card["title"], card["location"]),
            source="linkedin_search",
            company=card["company"],
            title=card["title"],
            location=card["location"],
            url=_JOB_VIEW_URL.format(job_id=card["job_id"]),
            description_raw=description,
            first_seen=now,
            last_seen=now,
            status="active",
        ))

    return jobs
