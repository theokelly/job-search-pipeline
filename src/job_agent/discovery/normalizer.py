from datetime import datetime, timezone
from ..db import Job, job_id


def normalize_greenhouse(company: str, raw: dict) -> Job:
    title = raw.get("title", "")
    location = raw.get("location", {}).get("name", "") if isinstance(raw.get("location"), dict) else str(raw.get("location", ""))
    url = raw.get("absolute_url", "")
    description = raw.get("content", "")
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    return Job(
        id=job_id(company, title, location),
        source="greenhouse_api",
        company=company,
        title=title,
        location=location,
        url=url,
        description_raw=description,
        first_seen=now,
        last_seen=now,
        status="active",
    )


def normalize_lever(company: str, raw: dict) -> Job:
    title = raw.get("text", "")
    categories = raw.get("categories", {})
    location = categories.get("location", "") if isinstance(categories, dict) else ""
    url = raw.get("hostedUrl", "")
    description_parts = raw.get("descriptionPlain", "") or raw.get("description", "")
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    return Job(
        id=job_id(company, title, location),
        source="lever_api",
        company=company,
        title=title,
        location=location,
        url=url,
        description_raw=description_parts,
        first_seen=now,
        last_seen=now,
        status="active",
    )


def normalize_ashby(company: str, raw: dict) -> Job:
    title = raw.get("title", "")
    # The posting API's current shape uses `location` (a string) plus
    # `secondaryLocations`; `locationName` is the retired field and is kept
    # only as a fallback for cached payloads and old test fixtures.
    location = raw.get("location") or raw.get("locationName") or ""
    secondary = [
        s.get("location", "")
        for s in raw.get("secondaryLocations") or []
        if isinstance(s, dict)
    ]
    secondary = [s for s in secondary if s]
    if secondary:
        location = "; ".join([location, *secondary]) if location else "; ".join(secondary)
    if raw.get("isRemote") and "remote" not in location.lower():
        location = f"{location} (Remote)".strip()
    url = raw.get("jobUrl", "")
    description = raw.get("descriptionHtml", "") or raw.get("descriptionPlain", "") or ""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    return Job(
        id=job_id(company, title, location),
        source="ashby_api",
        company=company,
        title=title,
        location=location,
        url=url,
        description_raw=description,
        first_seen=now,
        last_seen=now,
        status="active",
    )


def normalize_workday(company: str, host: str, site: str, posting: dict, detail: dict) -> Job:
    title = posting.get("title", "")
    location = posting.get("locationsText", "") or ""
    external_path = posting.get("externalPath", "")
    # Human-browsable posting URL. Most tenants serve it with or without a
    # locale prefix; the site-root form redirects correctly.
    url = f"https://{host}/{site}{external_path}" if external_path else ""
    info = (detail or {}).get("jobPostingInfo", {})
    description = info.get("jobDescription", "") or ""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    return Job(
        id=job_id(company, title, location),
        source="workday_api",
        company=company,
        title=title,
        location=location,
        url=url,
        description_raw=description,
        first_seen=now,
        last_seen=now,
        status="active",
    )


def normalize_fanout(extracted: dict) -> Job:
    """Build a Job from a manual.fetch_from_url extraction dict.

    Used by fan-out discovery for postings that are not on a recognized ATS
    (where no structured normalizer exists). Mirrors how the Streamlit
    Add-a-job flow builds Jobs from the same dict, with source search_fanout.
    """
    company = (extracted.get("company") or "Unknown").strip()
    title = (extracted.get("title") or "").strip()
    location = (extracted.get("location") or "").strip()
    url = (extracted.get("url") or "").strip()
    description = extracted.get("description") or ""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    return Job(
        id=job_id(company, title, location),
        source="search_fanout",
        company=company,
        title=title,
        location=location,
        url=url,
        description_raw=description,
        first_seen=now,
        last_seen=now,
        status="active",
    )
