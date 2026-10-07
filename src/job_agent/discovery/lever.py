import httpx
from ..db import Job
from .normalizer import normalize_lever


BASE = "https://api.lever.co/v0/postings/{company}"


def fetch_jobs(company: str, lever_slug: str) -> list[Job]:
    url = BASE.format(company=lever_slug)
    resp = httpx.get(url, params={"mode": "json"}, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    return [normalize_lever(company, posting) for posting in data]
