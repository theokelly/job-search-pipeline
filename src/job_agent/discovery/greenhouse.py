import httpx
from ..db import Job
from .normalizer import normalize_greenhouse


BASE = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs"


def fetch_jobs(company: str, board_token: str) -> list[Job]:
    url = BASE.format(token=board_token)
    resp = httpx.get(url, params={"content": "true"}, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    return [normalize_greenhouse(company, job) for job in data.get("jobs", [])]
