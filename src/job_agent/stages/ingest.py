"""Ingest individual posting URLs into the jobs DB.

The single-URL handoff path: the weekly scan (and any one-off find)
hands over posting URLs; each is fetched through the discovery
fanout's generic fetcher and upserted. This is the tested, first-class
version of what used to be a scratch script (the 2026-11-23 scan
ingest that brought in Coverflex, EPAM, et al.).
"""
from typing import Callable, Optional

from sqlmodel import select

from ..db import Job, get_session, init_db, upsert_job
from ..discovery.fanout import fetch_job_from_url
from ._common import _default_echo

INGESTED = "ingested"
ALREADY_PRESENT = "already_present"
FETCH_FAILED = "fetch_failed"


def run_ingest(
    urls: list[str],
    user_slug: str = "theo",
    score_after: bool = False,
    echo: Callable[[str], None] = _default_echo,
) -> list[tuple[str, str, Optional[Job]]]:
    """Fetch and upsert each URL. Returns (url, outcome, job|None) rows.

    Outcomes: INGESTED (new row), ALREADY_PRESENT (URL already in the
    DB — left untouched), FETCH_FAILED (posting closed or extraction
    failed). With score_after, the score stage runs afterwards over
    all unscored active jobs (in practice, the ones just ingested).
    """
    init_db()
    results: list[tuple[str, str, Optional[Job]]] = []
    with get_session() as session:
        for url in urls:
            existing = session.exec(select(Job).where(Job.url == url)).first()
            if existing is not None:
                results.append((url, ALREADY_PRESENT, existing))
                echo(f"  = {existing.company} — {existing.title} (already in DB)")
                continue
            job = fetch_job_from_url(url)
            if job is None:
                results.append((url, FETCH_FAILED, None))
                echo(f"  ! fetch failed (posting closed?): {url}")
                continue
            upsert_job(session, job)
            results.append((url, INGESTED, job))
            echo(f"  + {job.company} — {job.title}")
        session.commit()
    if score_after and any(outcome == INGESTED for _, outcome, _ in results):
        from .score import run_score
        run_score(user_slug=user_slug, echo=echo)
    return results
