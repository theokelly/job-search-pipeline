"""Tests for stages/single.py helpers (in-memory SQLite, no network).

mark_posting_status backs the UI's Mark-closed / Reopen buttons: it sets
Job.status for the clicked job and every cluster sibling, and nothing else.
"""
import pytest
from sqlmodel import SQLModel, Session, create_engine, select

from job_agent.db import Job
from job_agent.stages import single


@pytest.fixture()
def engine(tmp_path, monkeypatch):
    eng = create_engine(f"sqlite:///{tmp_path}/jobs.sqlite")
    SQLModel.metadata.create_all(eng)
    monkeypatch.setattr(single, "init_db", lambda: eng)
    monkeypatch.setattr(single, "get_session",
                        lambda: Session(eng, expire_on_commit=False))
    return eng


def _add(engine, jid, company, title, location, status="active"):
    with Session(engine) as s:
        s.add(Job(id=jid, source="x", company=company, title=title,
                  location=location, url="", description_raw="x",
                  status=status))
        s.commit()


def _statuses(engine):
    with Session(engine) as s:
        return {j.id: j.status for j in s.exec(select(Job)).all()}


def test_mark_closed_cascades_to_cluster_siblings(engine):
    _add(engine, "a1", "Acme", "Data Lead", "Amsterdam")
    _add(engine, "a2", "Acme", "Data Lead", "Berlin")
    _add(engine, "b1", "Beta", "Data Lead", "Amsterdam")

    updated = single.mark_posting_status("a1", "closed")

    assert sorted(updated) == ["a1", "a2"]
    assert _statuses(engine) == {"a1": "closed", "a2": "closed", "b1": "active"}


def test_reopen_sets_active(engine):
    _add(engine, "a1", "Acme", "Data Lead", "Amsterdam", status="closed")

    updated = single.mark_posting_status("a1", "active")

    assert updated == ["a1"]
    assert _statuses(engine) == {"a1": "active"}


def test_unknown_job_returns_empty(engine):
    assert single.mark_posting_status("nope", "closed") == []


def test_invalid_status_raises(engine):
    _add(engine, "a1", "Acme", "Data Lead", "Amsterdam")
    with pytest.raises(ValueError):
        single.mark_posting_status("a1", "banana")
