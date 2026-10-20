"""
Fetch a single job posting from a URL.

Cascade:
  1. JSON-LD JobPosting (Greenhouse, Lever, Ashby, Workday, many corporate sites)
  2. Next.js __NEXT_DATA__ blobs (Greenhouse-shaped job objects embedded by
     proxied careers sites, e.g. Vinted — those pages carry no JSON-LD)
  3. LinkedIn-specific selectors (reuse existing scraper)
  4. Generic HTML extraction (h1 / og:site_name / body text)

Returns a dict with title/company/location/description/url. Any field may be ""
on failure — the Streamlit UI surfaces a blank, editable form in that case so
the user can fall back to manual entry without losing the URL.
"""
import json
import re
import urllib.parse
import urllib.request
from typing import Optional

from bs4 import BeautifulSoup


_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

_EMPTY = {"title": "", "company": "", "location": "", "description": "", "url": ""}


# ---------------------------------------------------------------------------
# HTTP fetch
# ---------------------------------------------------------------------------

def _fetch_html(url: str, timeout: int = 15) -> Optional[BeautifulSoup]:
    try:
        req = urllib.request.Request(url, headers=_HEADERS)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return BeautifulSoup(resp.read(), "html.parser")
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Stage 1: JSON-LD JobPosting
# ---------------------------------------------------------------------------

def _extract_jsonld(soup: BeautifulSoup) -> dict:
    """Find a JSON-LD block of @type JobPosting and pull the standard fields."""
    out = dict(_EMPTY)
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        # JSON-LD can be a single object, a list, or wrapped in @graph
        candidates = []
        if isinstance(data, list):
            candidates.extend(data)
        elif isinstance(data, dict):
            if "@graph" in data and isinstance(data["@graph"], list):
                candidates.extend(data["@graph"])
            else:
                candidates.append(data)

        for obj in candidates:
            if not isinstance(obj, dict):
                continue
            obj_type = obj.get("@type", "")
            if isinstance(obj_type, list):
                is_jobposting = "JobPosting" in obj_type
            else:
                is_jobposting = obj_type == "JobPosting"
            if not is_jobposting:
                continue
            out["title"] = (obj.get("title") or "").strip()
            org = obj.get("hiringOrganization") or {}
            if isinstance(org, dict):
                out["company"] = (org.get("name") or "").strip()
            elif isinstance(org, str):
                out["company"] = org.strip()
            out["location"] = _location_from_jsonld(obj)
            # description may contain HTML
            desc = obj.get("description") or ""
            if desc:
                out["description"] = _strip_html(desc)[:20000]
            return out
    return out


def _location_from_jsonld(obj: dict) -> str:
    """Extract a readable location string from JobPosting.jobLocation (may be list/object)."""
    loc = obj.get("jobLocation")
    if not loc:
        return ""
    if isinstance(loc, list):
        parts = [_one_location(item) for item in loc]
        parts = [p for p in parts if p]
        return "; ".join(parts)
    return _one_location(loc)


def _one_location(loc) -> str:
    if isinstance(loc, str):
        return loc.strip()
    if not isinstance(loc, dict):
        return ""
    addr = loc.get("address")
    if not addr:
        return loc.get("name", "").strip()
    if isinstance(addr, str):
        return addr.strip()
    if isinstance(addr, dict):
        bits = [addr.get("addressLocality"), addr.get("addressRegion"), addr.get("addressCountry")]
        return ", ".join(b for b in bits if b)
    return ""


# ---------------------------------------------------------------------------
# Stage 2: Next.js __NEXT_DATA__ (Greenhouse-shaped job objects)
# ---------------------------------------------------------------------------
# Some careers sites (e.g. Vinted) are Next.js frontends over an ATS. The
# full job record ships as JSON in <script id="__NEXT_DATA__"> and there is
# no JSON-LD, so without this stage the cascade falls through to generic
# HTML extraction and mangles every field.

def _extract_next_data(soup: BeautifulSoup, url: str) -> dict:
    out = dict(_EMPTY)
    tag = soup.find("script", id="__NEXT_DATA__")
    if not tag or not tag.string:
        return out
    try:
        data = json.loads(tag.string)
    except (json.JSONDecodeError, TypeError):
        return out

    job = _next_data_job(data, url)
    if not job:
        return out

    out["title"] = (job.get("title") or "").strip()
    out["company"] = (job.get("company_name") or "").strip()
    loc = job.get("location")
    if isinstance(loc, dict):
        out["location"] = (loc.get("name") or "").strip()
    elif isinstance(loc, str):
        out["location"] = loc.strip()
    content = job.get("content") or ""
    if content:
        out["description"] = _strip_html(content)[:20000]
    return out


def _is_greenhouse_job(obj) -> bool:
    return (
        isinstance(obj, dict)
        and bool(obj.get("title"))
        and bool(obj.get("company_name"))
        and bool(obj.get("content"))
    )


def _next_data_job(data, url: str) -> Optional[dict]:
    """Locate the page's job object inside a __NEXT_DATA__ blob.

    Preference order:
      1. props.pageProps.job.data — the canonical slot on job pages.
      2. A Greenhouse-shaped object whose id matches the id in the URL
         (listing blobs embed many jobs; the first one found is typically
         a related job, not this page's).
      3. The only Greenhouse-shaped object, when there is exactly one.
    """
    try:
        cand = data["props"]["pageProps"]["job"]["data"]
        if _is_greenhouse_job(cand):
            return cand
    except (KeyError, TypeError):
        pass

    candidates: list = []

    def walk(o):
        if _is_greenhouse_job(o):
            candidates.append(o)
            return
        if isinstance(o, dict):
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(data)
    if not candidates:
        return None
    m = re.search(r"(\d{5,})", url)
    if m:
        url_id = m.group(1)
        for c in candidates:
            if str(c.get("id")) == url_id:
                return c
    if len(candidates) == 1:
        return candidates[0]
    return None


# ---------------------------------------------------------------------------
# Stage 3: LinkedIn-specific
# ---------------------------------------------------------------------------

_LINKEDIN_JOB_ID_RE = re.compile(r"/jobs/(?:view|comm/jobs/view)/(\d+)")


def _extract_linkedin(url: str) -> dict:
    """If the URL is a LinkedIn job page, use the existing scraper."""
    m = _LINKEDIN_JOB_ID_RE.search(url)
    if not m:
        return dict(_EMPTY)
    try:
        # Local import: avoid circular if anything in linkedin.py changes
        from .linkedin import _fetch_job_page  # type: ignore
    except ImportError:
        return dict(_EMPTY)
    description, meta = _fetch_job_page(m.group(1))
    public_url = f"https://www.linkedin.com/jobs/view/{m.group(1)}"
    return {
        "title": meta.get("title", ""),
        "company": meta.get("company", ""),
        "location": meta.get("location", ""),
        "description": description,
        "url": public_url,
    }


# ---------------------------------------------------------------------------
# Stage 4: Generic HTML
# ---------------------------------------------------------------------------

_BAD_DESC_PARENT_TAGS = {"nav", "footer", "header", "aside", "script", "style", "noscript"}


def _extract_generic(soup: BeautifulSoup, url: str) -> dict:
    """Best-effort fallback. h1 → title; og:site_name / domain → company; body → description."""
    out = dict(_EMPTY)

    h1 = soup.find("h1")
    if h1:
        out["title"] = h1.get_text(" ", strip=True)

    og_site = soup.find("meta", property="og:site_name")
    if og_site and og_site.get("content"):
        out["company"] = og_site["content"].strip()
    else:
        # Fall back to the domain (e.g. "booking.com")
        host = urllib.parse.urlparse(url).netloc
        host = host.removeprefix("www.").removeprefix("careers.").removeprefix("jobs.")
        if "." in host:
            host = host.split(".")[0]
        out["company"] = host.title() if host else ""

    # Description: prefer common JD containers; fall back to <main> or <body>
    desc_el = None
    for selector in [
        {"class_": re.compile(r"job[-_]?description", re.I)},
        {"class_": re.compile(r"description", re.I)},
        {"id": re.compile(r"job[-_]?description", re.I)},
        {"role": "main"},
    ]:
        desc_el = soup.find(["div", "section", "article"], **selector)
        if desc_el:
            break
    if not desc_el:
        desc_el = soup.find("main") or soup.find("body")

    if desc_el:
        # Strip noise
        for noisy in desc_el.find_all(_BAD_DESC_PARENT_TAGS):
            noisy.decompose()
        text = desc_el.get_text("\n", strip=True)
        # Collapse runs of blank lines
        text = re.sub(r"\n{3,}", "\n\n", text)
        out["description"] = text[:20000]

    return out


# ---------------------------------------------------------------------------
# HTML strip helper (for JSON-LD descriptions that contain raw HTML)
# ---------------------------------------------------------------------------

def _strip_html(text: str) -> str:
    """Robustly convert an HTML/entity-encoded blob into plain text.

    Handles the three common JSON-LD JobPosting shapes:
      - Plain text             (returned as-is)
      - HTML markup            (`<li>foo</li>`)            → tags removed via BS4
      - Entity-encoded HTML    (`&lt;li&gt;foo&lt;/li&gt;`) → entities decoded first, then stripped
      - Double-encoded         (`&amp;lt;li&amp;gt;…`)     → loop until stable
    """
    import html as _html_lib

    # 1. Unescape entities (may be double-encoded; loop until idempotent)
    for _ in range(3):
        decoded = _html_lib.unescape(text)
        if decoded == text:
            break
        text = decoded

    # 2. If anything that looks like a tag remains, parse + extract visible text
    if "<" in text and ">" in text:
        soup = BeautifulSoup(text, "html.parser")
        text = soup.get_text("\n", strip=True)

    # 3. Cleanup
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fetch_from_url(url: str) -> dict:
    """Fetch a job posting from a URL. Returns a dict with title, company,
    location, description, url. Fields are "" when extraction fails — caller
    should surface an editable form so the user can complete by hand.
    """
    url = url.strip()
    if not url:
        return dict(_EMPTY)

    # LinkedIn first (works without HTML parse on listing URL)
    li = _extract_linkedin(url)
    if any(li[k] for k in ("title", "description")):
        return _merge(_EMPTY, li, {"url": url})

    soup = _fetch_html(url)
    if soup is None:
        return _merge(_EMPTY, {"url": url})

    jsonld = _extract_jsonld(soup)
    if any(jsonld[k] for k in ("title", "description")):
        return _merge(_EMPTY, jsonld, {"url": url})

    next_data = _extract_next_data(soup, url)
    if any(next_data[k] for k in ("title", "description")):
        return _merge(_EMPTY, next_data, {"url": url})

    generic = _extract_generic(soup, url)
    return _merge(_EMPTY, generic, {"url": url})


def _merge(*dicts: dict) -> dict:
    """Right-wins merge; later dicts override earlier ones for non-empty values."""
    out: dict = {}
    for d in dicts:
        for k, v in d.items():
            if v or k not in out:
                out[k] = v
    return out
