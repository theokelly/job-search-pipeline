"""Tests for stages/ingest.py (temp SQLite, fetch stubbed — no network).

run_ingest is the scan-handoff path: fetch a posting URL through the
fanout fetcher and upsert it, reporting per-URL outcomes.
"""
import pytest
from sqlmodel import SQLModel, Session, create_engine, select

from job_agent.db import Job, job_id
from job_agent.stages import ingest


@pytest.fixture()
def engine(tmp_path, monkeypatch):
    eng = create_engine(f"sqlite:///{tmp_path}/jobs.sqlite")
    SQLModel.metadata.create_all(eng)
    monkeypatch.setattr(ingest, "init_db", lambda: eng)
    monkeypatch.setattr(ingest, "get_session",
                        lambda: Session(eng, expire_on_commit=False))
    return eng


def _job(url, company="TestCo", title="AI Lead"):
    return Job(id=job_id(company, title, "Amsterdam"), source="ingest",
               company=company, title=title, location="Amsterdam",
               url=url, description_raw="JD text", status="active")


def _rows(engine):
    with Session(engine) as s:
        return s.exec(select(Job)).all()


def test_ingest_new_url(engine, monkeypatch):
    url = "https://example.com/jobs/1"
    monkeypatch.setattr(ingest, "fetch_job_from_url", lambda u: _job(u))
    results = ingest.run_ingest([url], echo=lambda _: None)
    assert [(u, o) for u, o, _ in results] == [(url, ingest.INGESTED)]
    rows = _rows(engine)
    assert len(rows) == 1 and rows[0].url == url


def test_ingest_already_present_skips_fetch(engine, monkeypatch):
    url = "https://example.com/jobs/2"
    with Session(engine) as s:
        s.add(_job(url))
        s.commit()

    def _boom(u):
        raise AssertionError("fetch must not be called for a known URL")

    monkeypatch.setattr(ingest, "fetch_job_from_url", _boom)
    results = ingest.run_ingest([url], echo=lambda _: None)
    assert [o for _, o, _ in results] == [ingest.ALREADY_PRESENT]
    assert len(_rows(engine)) == 1


def test_ingest_fetch_failed(engine, monkeypatch):
    url = "https://example.com/jobs/gone"
    monkeypatch.setattr(ingest, "fetch_job_from_url", lambda u: None)
    results = ingest.run_ingest([url], echo=lambda _: None)
    assert [o for _, o, _ in results] == [ingest.FETCH_FAILED]
    assert _rows(engine) == []


def test_ingest_mixed_batch(engine, monkeypatch):
    known = "https://example.com/jobs/known"
    with Session(engine) as s:
        s.add(_job(known))
        s.commit()
    fresh = "https://example.com/jobs/fresh"
    gone = "https://example.com/jobs/gone"
    monkeypatch.setattr(
        ingest, "fetch_job_from_url",
        lambda u: _job(u, title="Platform Lead") if u == fresh else None)
    results = ingest.run_ingest([known, fresh, gone], echo=lambda _: None)
    assert [o for _, o, _ in results] == [
        ingest.ALREADY_PRESENT, ingest.INGESTED, ingest.FETCH_FAILED]
    assert len(_rows(engine)) == 2
