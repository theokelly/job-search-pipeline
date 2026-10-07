"""
Phase 5: Gmail alert ingestion via IMAP + App Password.

Setup (one-time):
  1. Enable IMAP in Gmail: Settings → See all settings → Forwarding and POP/IMAP → Enable IMAP
  2. Generate an App Password: Google Account → Security → App passwords → select Mail
  3. Add to .env:
       GMAIL_USER=you@gmail.com
       GMAIL_APP_PASSWORD=xxxx xxxx xxxx xxxx
  4. In Gmail, create filters to label job alert emails:
       From: jobalerts@linkedin.com  → label: Job Alerts/LinkedIn
       From: jobalert@indeed.com     → label: Job Alerts/Indeed
       From: alerts@glassdoor.com    → label: Job Alerts/Glassdoor

LinkedIn strategy: alert emails contain a description snippet (~150 words) directly in the
body. The pipeline also attempts to fetch the full JD from the public LinkedIn URL
(linkedin.com/jobs/view/{id}) — this works when LinkedIn serves it without login, and
gracefully falls back to the email snippet when it doesn't.
"""
import os
import imaplib
import email as _email_lib
import re
import html as _html_lib
import urllib.request
import urllib.error
from datetime import datetime
from typing import Optional

from ..db import Job, job_id as make_job_id


# ---------------------------------------------------------------------------
# IMAP connection
# ---------------------------------------------------------------------------

def _connect() -> imaplib.IMAP4_SSL:
    user = os.environ.get("GMAIL_USER", "")
    password = os.environ.get("GMAIL_APP_PASSWORD", "")
    if not user or not password:
        raise EnvironmentError(
            "Gmail IMAP not configured. Set GMAIL_USER and GMAIL_APP_PASSWORD in .env. "
            "Generate an App Password at: Google Account → Security → App passwords."
        )
    conn = imaplib.IMAP4_SSL("imap.gmail.com", 993)
    conn.login(user, password)
    return conn


# ---------------------------------------------------------------------------
# Email body extraction
# ---------------------------------------------------------------------------

def _get_body(msg: _email_lib.message.Message) -> tuple[str, str]:
    """Return (plain_text, html_text) from a parsed email message."""
    plain, html = "", ""
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            charset = part.get_content_charset() or "utf-8"
            payload = part.get_payload(decode=True)
            if payload is None:
                continue
            decoded = payload.decode(charset, errors="replace")
            if ct == "text/plain" and not plain:
                plain = decoded
            elif ct == "text/html" and not html:
                html = decoded
    else:
        charset = msg.get_content_charset() or "utf-8"
        payload = msg.get_payload(decode=True)
        if payload:
            decoded = payload.decode(charset, errors="replace")
            if msg.get_content_type() == "text/html":
                html = decoded
            else:
                plain = decoded
    return plain, html


# ---------------------------------------------------------------------------
# HTML stripping
# ---------------------------------------------------------------------------

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_EXCESS_SPACE_RE = re.compile(r"[ \t]{2,}")
_EXCESS_NEWLINE_RE = re.compile(r"\n{3,}")


def _strip_html(text: str) -> str:
    text = _HTML_TAG_RE.sub(" ", text)
    text = _html_lib.unescape(text)
    text = _EXCESS_SPACE_RE.sub(" ", text)
    text = _EXCESS_NEWLINE_RE.sub("\n\n", text)
    return text.strip()


# ---------------------------------------------------------------------------
# URL extraction from HTML email body
# ---------------------------------------------------------------------------

# LinkedIn alert emails use /comm/jobs/view/{id} — we extract the numeric job ID
# and reconstruct the public-facing URL for the fetch attempt.
_LINKEDIN_COMM_RE = re.compile(
    r'https?://www\.linkedin\.com/comm/jobs/view/(\d+)',
    re.IGNORECASE,
)
_INDEED_URL_RE = re.compile(
    r'href="(https?://(?:[a-z]{2}\.)?indeed\.com/(?:viewjob|rc/clk|applystart)[^"]+)"',
    re.IGNORECASE,
)
_GLASSDOOR_URL_RE = re.compile(
    r'href="(https?://(?:www\.)?glassdoor\.(?:com|nl|co\.uk)/job-listing/[^"]+)"',
    re.IGNORECASE,
)


def _extract_job_urls(html_body: str, source: str) -> list[str]:
    """Extract job URLs from HTML email. Returns public-facing URLs where possible."""
    if source == "linkedin":
        job_ids = _LINKEDIN_COMM_RE.findall(html_body)
        seen: set[str] = set()
        result = []
        for jid in job_ids:
            url = f"https://www.linkedin.com/jobs/view/{jid}"
            if url not in seen:
                seen.add(url)
                result.append(url)
        return result

    urls = []
    for pattern in (_INDEED_URL_RE, _GLASSDOOR_URL_RE):
        urls.extend(pattern.findall(html_body))
    seen2: set[str] = set()
    result2 = []
    for u in urls:
        if u not in seen2:
            seen2.add(u)
            result2.append(u)
    return result2


def _extract_linkedin_jobs_from_html(html_body: str) -> list[dict]:
    """
    Extract LinkedIn job URLs from alert email HTML.

    Only extracts the job URL — title, company, location, and description are
    fetched from the individual LinkedIn job page (via _fetch_job_page in
    linkedin.py) rather than parsed from the email HTML, which is fragile and
    changes frequently.

    Returns a list of dicts with keys: url, title, company, location.
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html_body, "html.parser")
    jobs: list[dict] = []
    seen_ids: set[str] = set()

    for a_tag in soup.find_all("a", href=_LINKEDIN_COMM_RE):
        href = a_tag.get("href", "")
        m = re.search(r"/jobs/view/(\d+)", href)
        if not m or m.group(1) in seen_ids:
            continue
        job_id = m.group(1)
        seen_ids.add(job_id)
        jobs.append({
            "url": f"https://www.linkedin.com/jobs/view/{job_id}",
            "title": "",
            "company": "",
            "location": "",
            "_linkedin_id": job_id,
        })

    return jobs


# ---------------------------------------------------------------------------
# JD fetching
# ---------------------------------------------------------------------------

_LINKEDIN_LOGIN_SIGNALS = ("authwall", "sign in to", "join now", "uas/login")


def _fetch_jd(url: str) -> tuple[str, dict]:
    """Fetch job description and metadata from a public job URL.

    Returns (description_text, metadata_dict).
    metadata_dict may contain "title", "company", "location" for LinkedIn pages.
    """
    from bs4 import BeautifulSoup
    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            content = resp.read()

        if "linkedin.com" in url:
            soup = BeautifulSoup(content, "html.parser")
            description = ""
            div = soup.find("div", class_="description__text description__text--rich")
            if div:
                for el in div.find_all(["span", "a"]):
                    el.decompose()
                description = div.get_text(separator="\n").strip()[:8000]

            meta: dict = {}
            title_el = (
                soup.find("h1", class_=lambda c: c and "top-card-layout__title" in c)
                or soup.find("h2", class_=lambda c: c and "top-card-layout__title" in c)
                or soup.find("h1", class_=lambda c: c and "topcard__title" in c)
            )
            company_el = (
                soup.find("a", class_=lambda c: c and "topcard__org-name-link" in c)
                or soup.find("span", class_=lambda c: c and "topcard__org-name-link" in c)
            )
            location_el = soup.find("span", class_=lambda c: c and "topcard__flavor--bullet" in c)

            if title_el:
                meta["title"] = title_el.get_text(" ", strip=True)
            if company_el:
                meta["company"] = company_el.get_text(" ", strip=True)
            if location_el:
                meta["location"] = location_el.get_text(" ", strip=True)

            return description, meta

        return _strip_html(content.decode("utf-8", errors="replace"))[:8000], {}
    except Exception:
        return "", {}


# ---------------------------------------------------------------------------
# LinkedIn description snippet extraction from email
# ---------------------------------------------------------------------------

# Updated 2026-10: London/UK removed (on-site London no longer in scope).
# Kept loose on purpose; the pre-filter applies the real commute/remote rules.
_LOCATION_SIGNALS = ("amsterdam", "netherlands", "nederland", "remote", "emea", "europe",
                     "amstelveen", "schiphol", "hoofddorp", "haarlem", "almere", "zaandam",
                     "utrecht", "hilversum")

# Matches title/company/location blocks in plain-text job alert emails.
# Handles both \n and \r\n line endings. Location can be short ("Amsterdam")
# or longer ("Amsterdam, Netherlands · €80,000").
_PLAIN_BLOCK_RE = re.compile(
    r"^(?P<title>[A-Z][^\r\n]{4,79})\r?\n"
    r"(?P<company>[^\r\n]{2,59})\r?\n"
    r"(?P<location>[^\r\n]{3,79})",
    re.MULTILINE,
)


def _extract_metadata(plain: str) -> list[dict]:
    """
    Extract title/company/location from plain-text job alert email body.
    Only returns blocks where the location signals Amsterdam/NL/Remote.
    """
    results = []
    for m in _PLAIN_BLOCK_RE.finditer(plain):
        location = m.group("location").strip()
        if any(kw in location.lower() for kw in _LOCATION_SIGNALS):
            results.append({
                "title": m.group("title").strip(),
                "company": m.group("company").strip(),
                "location": location,
            })
    return results


# ---------------------------------------------------------------------------
# Per-label ingestion
# ---------------------------------------------------------------------------

def _fetch_from_label(
    conn: imaplib.IMAP4_SSL,
    label: str,
    source: str,
    since_days: int,
    known_ids: set[str] | None = None,
    refetch_jds: bool = False,
) -> list[Job]:
    try:
        status, _ = conn.select(f'"{label}"', readonly=True)
        if status != "OK":
            return []
    except Exception:
        return []

    from datetime import timedelta
    since_date = (datetime.utcnow() - timedelta(days=since_days)).strftime("%d-%b-%Y")
    _, data = conn.search(None, f'SINCE {since_date}')
    if not data[0]:
        return []

    jobs: list[Job] = []
    now = datetime.utcnow()
    for num in data[0].split():
        _, raw_data = conn.fetch(num, "(RFC822)")
        msg = _email_lib.message_from_bytes(raw_data[0][1])
        plain, html_body = _get_body(msg)

        # For LinkedIn, parse the email HTML to extract job cards (URL + metadata together)
        if source == "linkedin" and html_body:
            html_cards = _extract_linkedin_jobs_from_html(html_body)
        else:
            html_cards = []

        if html_cards:
            from .linkedin import _fetch_job_page
            import time as _time
            for card in html_cards:
                url = card["url"]
                linkedin_id = card.get("_linkedin_id", "")

                # Dedup via LinkedIn job ID against known DB entries.
                # We can't compute a proper job_id (company+title+location hash)
                # until after fetching the page, so we check the URL directly.
                skip_fetch = (
                    known_ids is not None
                    and not refetch_jds
                    and any(linkedin_id in kid for kid in known_ids)
                ) if linkedin_id else False

                if skip_fetch:
                    description, page_meta = "", {}
                else:
                    # Fetch title, company, location, and description from the
                    # individual LinkedIn posting — more reliable than email HTML.
                    description, page_meta = _fetch_job_page(linkedin_id)
                    _time.sleep(1.0)  # be polite to LinkedIn

                title    = page_meta.get("title", "")
                company  = page_meta.get("company", "Unknown")
                location = page_meta.get("location", "Amsterdam, Netherlands")
                if not title:
                    title = "Unknown Role"

                jobs.append(Job(
                    id=make_job_id(company, title, location),
                    source=source,
                    company=company,
                    title=title,
                    location=location,
                    url=url,
                    description_raw=description,
                    first_seen=now,
                    last_seen=now,
                    status="active",
                ))
        elif source != "linkedin":
            # Non-LinkedIn sources: fall back to URL-only extraction + plain-text metadata
            job_urls = _extract_job_urls(html_body, source) if html_body else []
            meta_blocks = _extract_metadata(plain) if plain else []
            for i, url in enumerate(job_urls):
                email_meta = meta_blocks[i] if i < len(meta_blocks) else {}

                # known_ids dedup: same logic as the LinkedIn path
                e_title    = email_meta.get("title", "")
                e_company  = email_meta.get("company", "") or "Unknown"
                e_location = email_meta.get("location", "") or "Amsterdam, Netherlands"
                tentative_id = make_job_id(e_company, e_title or "Unknown Role", e_location)
                skip_fetch = (known_ids is not None
                              and tentative_id in known_ids
                              and not refetch_jds)

                if skip_fetch:
                    description, page_meta = "", {}
                else:
                    description, page_meta = _fetch_jd(url)

                title    = e_title    or page_meta.get("title", "")
                company  = email_meta.get("company", "")  or page_meta.get("company", "Unknown")
                location = email_meta.get("location", "") or page_meta.get("location", "Amsterdam, Netherlands")
                if not title:
                    title = "Unknown Role"
                jobs.append(Job(
                    id=make_job_id(company, title, location),
                    source=source,
                    company=company,
                    title=title,
                    location=location,
                    url=url,
                    description_raw=description,
                    first_seen=now,
                    last_seen=now,
                    status="active",
                ))
        # If neither html_cards nor url-based extraction found anything,
        # fall back to plain-text metadata blocks (no URL, no JD — lowest quality)
        elif source != "linkedin" and not html_cards:
            meta_blocks = _extract_metadata(plain) if plain else []
            for meta in meta_blocks:
                jobs.append(Job(
                    id=make_job_id(meta["company"], meta["title"], meta["location"]),
                    source=source,
                    company=meta["company"],
                    title=meta["title"],
                    location=meta["location"],
                    url="",
                    description_raw="",
                    first_seen=now,
                    last_seen=now,
                    status="active",
                ))

    return jobs


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

_DEFAULT_LABELS: dict[str, str] = {
    "Job Alerts/LinkedIn": "linkedin",
    "Job Alerts/Indeed": "indeed",
    "Job Alerts/Glassdoor": "glassdoor",
}


def fetch_alert_jobs(
    labels: Optional[dict[str, str]] = None,
    since_days: int = 14,
    known_ids: set[str] | None = None,
    refetch_jds: bool = False,
) -> list[Job]:
    """
    Poll Gmail IMAP for job alert emails from the last `since_days` days.

    Deduplication is handled by the DB upsert layer, so re-running is safe.
    labels: {gmail_label_name: source_tag}. Defaults to LinkedIn, Indeed, and Glassdoor.
    known_ids: set of job IDs we already have in the DB *with descriptions* — JD
        network fetch is skipped for these (matches the LinkedIn discovery pattern).
    refetch_jds: when True, ignore known_ids and re-fetch every JD. Use when prior
        fetches were imperfect and you want a clean re-pull.
    Raises EnvironmentError if GMAIL_USER or GMAIL_APP_PASSWORD are not set.
    """
    if labels is None:
        labels = _DEFAULT_LABELS

    conn = _connect()
    all_jobs: list[Job] = []
    try:
        for label, source in labels.items():
            fetched = _fetch_from_label(
                conn, label, source,
                since_days=since_days,
                known_ids=known_ids,
                refetch_jds=refetch_jds,
            )
            all_jobs.extend(fetched)
    finally:
        conn.logout()

    return all_jobs
