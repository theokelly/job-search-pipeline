"""Tests for job_agent.mutations — the UI write operations (Phase I-3).

Fixture pattern from test_queries.py: monkeypatch the module's
init_db/get_session onto a tmp sqlite engine.
"""
import pytest
from sqlmodel import SQLModel, Session, create_engine, select

from job_agent import mutations
from job_agent.db import Job, User, UserJobMeta


@pytest.fixture()
def engine(tmp_path, monkeypatch):
    eng = create_engine(f"sqlite:///{tmp_path}/jobs.sqlite")
    SQLModel.metadata.create_all(eng)
    monkeypatch.setattr(mutations, "init_db", lambda *a, **k: eng)
    monkeypatch.setattr(mutations, "get_session",
                        lambda *a, **k: Session(eng, expire_on_commit=False))
    return eng


def _user(engine):
    with Session(engine) as s:
        u = User(slug="theo", profile_path="profile/theo.yaml")
        s.add(u)
        s.commit()
        s.refresh(u)
        return u.id


def _job(jid, company="Acme", title="Head of AI", location="Amsterdam"):
    return Job(id=jid, source="x", company=company, title=title,
               location=location, url="", description_raw="x", status="active")


def test_save_job_fields_strips_and_updates(engine):
    with Session(engine) as s:
        s.add(_job("j1"))
        s.commit()
    mutations.save_job_fields("j1", "  Beta ", " AI Lead ", " Utrecht ")
    with Session(engine) as s:
        j = s.get(Job, "j1")
        assert (j.company, j.title, j.location) == ("Beta", "AI Lead", "Utrecht")
    # Unknown job: no-op, no error.
    mutations.save_job_fields("nope", "A", "B", "C")


def test_set_app_status_propagates_to_cluster_siblings(engine):
    uid = _user(engine)
    with Session(engine) as s:
        s.add(_job("a1"))
        s.add(_job("a2"))  # same company+title+location -> same cluster
        s.add(_job("b1", company="Beta", title="Data Lead"))
        s.commit()
    mutations.set_app_status("a1", uid, "applied")
    with Session(engine) as s:
        metas = {m.job_id: m for m in s.exec(select(UserJobMeta)).all()}
    assert metas["a1"].app_status == "applied"
    assert metas["a2"].app_status == "applied"  # sibling followed
    assert "b1" not in metas  # other cluster untouched
    assert metas["a1"].app_status_at is not None

    mutations.set_app_status("a1", uid, "clear")
    with Session(engine) as s:
        metas = {m.job_id: m for m in s.exec(select(UserJobMeta)).all()}
    assert metas["a1"].app_status is None and metas["a2"].app_status is None


def test_set_app_status_unknown_job_is_noop(engine):
    uid = _user(engine)
    mutations.set_app_status("nope", uid, "applied")


def test_save_manual_job_defaults_and_idempotent_id(engine):
    jid = mutations.save_manual_job("", "", "", "desc", " https://x/1 ")
    with Session(engine) as s:
        j = s.get(Job, jid)
        assert j.company == "Unknown" and j.title == "Unknown Role"
        assert j.source == "manual" and j.status == "active"
        assert j.url == "https://x/1"
    # Same identity fields -> same id (upsert, not duplicate).
    assert mutations.save_manual_job("", "", "", "other", "https://x/2") == jid
