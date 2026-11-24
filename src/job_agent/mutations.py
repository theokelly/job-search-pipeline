"""Write operations behind the Streamlit UI (moved from
app/streamlit_app.py, Phase I-3) — the write-side counterpart to
queries.py. Pure DB mutations with no streamlit imports, so they are
unit-testable against a fixture database (tests/test_mutations.py).
The app wraps them and clears st.cache_data after each call; cache
invalidation is a view concern and stays in the view.
"""
from datetime import datetime, timezone

from sqlmodel import select

from . import clustering
from .db import (
    Job, get_or_create_meta, get_session, init_db,
)
from .db import job_id as compute_job_id
from .db import upsert_job


def save_job_fields(job_id: str, company: str, title: str, location: str):
    init_db()
    with get_session() as s:
        job = s.get(Job, job_id)
        if job:
            job.company = company.strip()
            job.title = title.strip()
            job.location = location.strip()
            s.add(job)
            s.commit()


def set_app_status(job_id: str, user_id: int, status: str):
    """Update app_status for the clicked job AND every sibling in its cluster.

    Siblings represent the same logical role posted across geographies; the user
    only applies once, so the state should be consistent across all variants.
    """
    init_db()
    with get_session() as s:
        clicked = s.get(Job, job_id)
        if not clicked:
            return
        target_key = clustering.cluster_key(clicked)
        all_jobs = s.exec(select(Job)).all()
        sibling_ids = [j.id for j in all_jobs if clustering.cluster_key(j) == target_key]

        new_status = status if status != "clear" else None
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        for jid in sibling_ids:
            meta = get_or_create_meta(s, jid, user_id)
            meta.app_status = new_status
            meta.app_status_at = now
            meta.updated_at = now
            s.add(meta)
        s.commit()


def save_manual_job(company: str, title: str, location: str, description: str, url: str) -> str:
    """Insert/update a Job row from manual entry. Returns the job_id."""
    init_db()
    company = (company or "Unknown").strip()
    title = (title or "Unknown Role").strip()
    location = (location or "").strip()
    job_db_id = compute_job_id(company, title, location)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    job = Job(
        id=job_db_id,
        source="manual",
        company=company,
        title=title,
        location=location,
        url=url.strip(),
        description_raw=description or "",
        first_seen=now,
        last_seen=now,
        status="active",
    )
    with get_session() as s:
        upsert_job(s, job)
        s.commit()
    return job_db_id
