import httpx
from ..db import Job
from .normalizer import normalize_ashby


BASE = "https://api.ashbyhq.com/posting-api/job-board/{board}"


def fetch_jobs(company: str, board: str) -> list[Job]:
    """Fetch open postings from a company's public Ashby job board.

    `board` is the organization host in the board URL, e.g. for
    https://jobs.ashbyhq.com/acme it is "acme". No auth required.
    """
    url = BASE.format(board=board)
    resp = httpx.get(url, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    return [normalize_ashby(company, job) for job in data.get("jobs", [])]
