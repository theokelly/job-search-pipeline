"""Read models for the Streamlit UI (moved from app/streamlit_app.py, Phase F).

Pure query functions over the DB — no streamlit imports, so they are
unit-testable against a fixture database (tests/test_queries.py). The
app wraps them in st.cache_data; caching is a view concern and stays
in the view module.
"""
from datetime import datetime

import pandas as pd
from sqlmodel import select

from .db import (
    Application, Job, Score, User, UserJobMeta, get_session, init_db,
)
from .tiers import STRONG_FIT, WORTH_REVIEWING


SOURCE_LABELS = {
    "linkedin_search": "LinkedIn Search",
    "linkedin": "LinkedIn Alert",
    "greenhouse": "Greenhouse",
    "greenhouse_api": "Greenhouse",
    "lever": "Lever",
    "indeed": "Indeed",
    "glassdoor": "Glassdoor",
}


def load_stats_timeseries():
    """Temporal data for the Stats-page journey view.

    Deliberately independent of the active/inactive sidebar toggle — the journey
    is the whole search history, so every posting (active, closed, expired) counts.
    Returns a dict of small DataFrames plus the funnel/cost counts.
    """
    import re
    _date_rx = re.compile(r"/(\d{4}-\d{2}-\d{2})_")

    init_db()
    with get_session() as s:
        user = s.exec(select(User).where(User.slug == "theo")).first()
        uid = user.id if user else None

        jobs = s.exec(select(Job)).all()
        scores = s.exec(select(Score).where(Score.user_id == uid)).all() if uid else []
        metas = s.exec(select(UserJobMeta).where(UserJobMeta.user_id == uid)).all() if uid else []
        apps = s.exec(select(Application).where(Application.user_id == uid)).all() if uid else []

        job_by_id = {j.id: j for j in jobs}

        # --- Discovery: one row per posting, keyed by first_seen ---
        discovery = pd.DataFrame([
            {"date": j.first_seen, "source": SOURCE_LABELS.get(j.source, j.source)}
            for j in jobs if j.first_seen
        ])

        # --- Scoring: latest score per job (avoid double-counting re-scores) ---
        latest_score: dict[str, Score] = {}
        for sc in sorted(scores, key=lambda x: x.scored_at or datetime.min, reverse=True):
            if sc.job_id not in latest_score:
                latest_score[sc.job_id] = sc
        scoring = pd.DataFrame([
            {"date": sc.scored_at, "tier": sc.tier}
            for sc in latest_score.values() if sc.scored_at
        ])

        # --- Generation: date parsed from the materials_path prefix ---
        gen_rows = []
        for a in apps:
            m = _date_rx.search(a.materials_path or "")
            if not m:
                continue
            tier = latest_score[a.job_id].tier if a.job_id in latest_score else None
            gen_rows.append({"date": pd.to_datetime(m.group(1)), "tier": tier})
        generation = pd.DataFrame(gen_rows)

        # --- Application journey: current tracked status + when it was set ---
        journey_rows = []
        for m in metas:
            if not m.app_status or not m.app_status_at:
                continue
            job = job_by_id.get(m.job_id)
            sc = latest_score.get(m.job_id)
            journey_rows.append({
                "date": m.app_status_at,
                "app_status": m.app_status,
                "company": job.company if job else "—",
                "title": job.title if job else "—",
                "first_seen": job.first_seen if job else None,
                "tier": sc.tier if sc else None,
            })
        journey = pd.DataFrame(journey_rows)

        # Prescreen decisions (for the funnel; no clean per-decision timestamp).
        # Every screened job costs one Haiku call, approved or not.
        prescreen_approved = sum(
            1 for m in metas if m.prescreen_status in ("approved", "manual_override")
        )
        prescreen_total = sum(1 for m in metas if m.prescreen_status)

    # Normalise date columns to pandas datetimes
    for frame in (discovery, scoring, generation, journey):
        if not frame.empty:
            frame["date"] = pd.to_datetime(frame["date"])

    # --- Funnel: distinct-job counts per stage ---
    strong = int((scoring["tier"] == STRONG_FIT).sum()) if not scoring.empty else 0
    worth = int((scoring["tier"] == WORTH_REVIEWING).sum()) if not scoring.empty else 0
    applied_now = int((journey["app_status"] == "applied").sum()) if not journey.empty else 0
    funnel = [
        ("Discovered",        len(jobs)),
        ("Prescreen passed",  prescreen_approved),
        ("Scored",            len(latest_score)),
        ("Strong / Worth",    strong + worth),
        ("Materials made",    len(generation)),
        ("Actioned",          len(journey)),
        ("Applied",           applied_now),
    ]

    # --- Cost inputs: per-stage call volumes and generation split by tier ---
    gen_strong = int((generation["tier"] == STRONG_FIT).sum()) if not generation.empty else 0
    counts = {
        "prescreen": prescreen_total,
        "score": len(latest_score),
        "cover_letter_strong": gen_strong,
        "cover_letter_other": len(generation) - gen_strong,
        "resume_rec": len(generation),
    }

    return {
        "discovery": discovery,
        "scoring": scoring,
        "generation": generation,
        "journey": journey,
        "funnel": funnel,
        "cost_counts": counts,
    }
