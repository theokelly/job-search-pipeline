"""Tests for job_agent.queries — the UI read models (Phase F).

Fixture DB in tmp (pattern from test_single.py): monkeypatch the
module's init_db/get_session so the loaders hit an in-memory-ish
sqlite file, never the real data/jobs.sqlite.
"""
from datetime import datetime

import pytest
from sqlmodel import SQLModel, Session, create_engine

from job_agent import queries
from job_agent.db import Application, Job, Score, User, UserJobMeta
from job_agent.tiers import STRONG_FIT, WORTH_REVIEWING


@pytest.fixture()
def engine(tmp_path, monkeypatch):
    eng = create_engine(f"sqlite:///{tmp_path}/jobs.sqlite")
    SQLModel.metadata.create_all(eng)
    monkeypatch.setattr(queries, "init_db", lambda *a, **k: eng)
    monkeypatch.setattr(queries, "get_session",
                        lambda *a, **k: Session(eng, expire_on_commit=False))
    monkeypatch.setattr(queries, "load_profile", lambda *a, **k: {})
    return eng


def _seed_user(engine):
    with Session(engine) as s:
        u = User(slug="theo", profile_path="profile/theo.yaml")
        s.add(u)
        s.commit()
        s.refresh(u)
        return u.id


def _score(job_id, uid, tier, when):
    return Score(job_id=job_id, user_id=uid, tier=tier, skill_fit=50,
                 preference_alignment=50, recommended_resume_version="base",
                 gap_assessment="g", positioning_notes="p", rationale="r",
                 raw_response="{}", scored_at=when)


def _job(jid, company="Acme", title="Head of AI", source="greenhouse",
         status="active", first_seen=datetime(2026, 11, 1, 9, 0),
         location="Amsterdam, Netherlands", description="x" * 150):
    return Job(id=jid, source=source, company=company, title=title,
               location=location, url=f"https://x/{jid}",
               description_raw=description, status=status,
               first_seen=first_seen)


def test_stats_timeseries_funnel_and_latest_score(engine):
    uid = _seed_user(engine)
    with Session(engine) as s:
        s.add(_job("j1"))
        s.add(_job("j2", source="linkedin_search",
                   first_seen=datetime(2026, 11, 2, 9, 0)))
        # j1 scored twice — only the latest counts.
        s.add(_score("j1", uid, WORTH_REVIEWING, datetime(2026, 11, 3, 9, 0)))
        s.add(_score("j1", uid, STRONG_FIT, datetime(2026, 11, 4, 9, 0)))
        s.add(UserJobMeta(job_id="j1", user_id=uid, prescreen_status="approved",
                          app_status="applied",
                          app_status_at=datetime(2026, 11, 5, 9, 0)))
        s.add(UserJobMeta(job_id="j2", user_id=uid, prescreen_status="rejected"))
        s.add(Application(job_id="j1", user_id=uid,
                          materials_path="outputs/theo/2026-11-20_acme_head-of-ai/x"))
        s.commit()

    out = queries.load_stats_timeseries()
    funnel = dict(out["funnel"])
    assert funnel["Discovered"] == 2
    assert funnel["Prescreen passed"] == 1
    assert funnel["Scored"] == 1
    assert funnel["Strong / Worth"] == 1  # latest score only
    assert funnel["Materials made"] == 1
    assert funnel["Applied"] == 1
    assert out["cost_counts"]["prescreen"] == 2
    assert out["cost_counts"]["cover_letter_strong"] == 1
    sources = set(out["discovery"]["source"])
    assert sources == {"Greenhouse", "LinkedIn Search"}  # SOURCE_LABELS applied
    journey = out["journey"]
    assert len(journey) == 1 and journey.iloc[0]["company"] == "Acme"


def test_stats_timeseries_empty_db(engine):
    _seed_user(engine)
    out = queries.load_stats_timeseries()
    assert dict(out["funnel"])["Discovered"] == 0
    assert out["cost_counts"]["score"] == 0


def test_load_jobs_df_tiers_clusters_and_inactive(engine):
    uid = _seed_user(engine)
    with Session(engine) as s:
        # Same (company, title) cluster, two postings — one canonical.
        s.add(_job("a1", location="Amsterdam, Netherlands"))
        s.add(_job("a2", location="Remote - Netherlands"))
        s.add(_job("b1", company="Beta", title="AI Lead"))
        s.add(_job("c1", company="Core", title="Data Lead", status="closed"))
        s.add(_score("a1", uid, STRONG_FIT, datetime(2026, 11, 4, 9, 0)))
        s.add(UserJobMeta(job_id="b1", user_id=uid, prescreen_status="rejected",
                          prescreen_reason="geography"))
        s.commit()

    df, user_id = queries.load_jobs_df()
    assert user_id == uid
    assert set(df["id"]) == {"a1", "a2", "b1"}  # closed excluded by default
    rows = {r["id"]: r for _, r in df.iterrows()}
    assert rows["a1"]["display_tier"] == STRONG_FIT
    assert rows["b1"]["display_tier"] == "Screened Out"
    assert rows["a2"]["display_tier"] == "Unscored"
    assert rows["a1"]["city"] == "Amsterdam"
    assert rows["a2"]["city"] == "Remote"
    assert rows["a1"]["has_jd"] is True or rows["a1"]["has_jd"] == True  # 150-char JD
    # Exactly one canonical per cluster.
    assert int(df["is_canonical"].sum()) == 2

    df_all, _ = queries.load_jobs_df(include_inactive=True)
    assert "c1" in set(df_all["id"])


def test_load_job_detail_returns_latest_score_and_parts(engine):
    uid = _seed_user(engine)
    with Session(engine) as s:
        s.add(_job("j1"))
        s.add(_score("j1", uid, WORTH_REVIEWING, datetime(2026, 11, 3, 9, 0)))
        s.add(_score("j1", uid, STRONG_FIT, datetime(2026, 11, 4, 9, 0)))
        s.add(UserJobMeta(job_id="j1", user_id=uid, prescreen_status="approved"))
        s.add(Application(job_id="j1", user_id=uid, materials_path="outputs/x"))
        s.commit()

    job, sc, meta, app, user_id = queries.load_job_detail("j1")
    assert job.id == "j1" and user_id == uid
    assert sc.tier == STRONG_FIT  # latest
    assert meta.prescreen_status == "approved"
    assert app.materials_path == "outputs/x"

    job2, sc2, meta2, app2, _ = queries.load_job_detail("nope")
    assert job2 is None and sc2 is None and meta2 is None and app2 is None
