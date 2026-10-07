from datetime import datetime
from ..db import Job, job_id


def normalize_greenhouse(company: str, raw: dict) -> Job:
    title = raw.get("title", "")
    location = raw.get("location", {}).get("name", "") if isinstance(raw.get("location"), dict) else str(raw.get("location", ""))
    url = raw.get("absolute_url", "")
    description = raw.get("content", "")
    now = datetime.utcnow()
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
    now = datetime.utcnow()
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
