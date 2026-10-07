"""_plan_scoring must not copy an existing score back when rescoring."""
from datetime import datetime
from sqlmodel import SQLModel, Session, create_engine
from job_agent.db import Job, Score, job_id
from job_agent.pipeline import _plan_scoring


def _job(title, loc):
    return Job(id=job_id("Acme", title, loc), source="x", company="Acme", title=title,
               location=loc, url="", description_raw="x" * 200,
               first_seen=datetime.utcnow(), last_seen=datetime.utcnow())


def _setup():
    eng = create_engine("sqlite://")
    SQLModel.metadata.create_all(eng)
    s = Session(eng)
    j = _job("Head of AI", "Amsterdam")
    s.add(j)
    s.add(Score(job_id=j.id, user_id=1, tier="Worth Reviewing", skill_fit=70, preference_alignment=60,
                recommended_resume_version="ai_focused", gap_assessment="", positioning_notes="",
                rationale="", raw_response=""))
    s.commit()
    return s, j


def test_default_copies_existing():
    s, j = _setup()
    canon, _, copies = _plan_scoring(s, 1, [j], {}, lambda m: None)
    assert canon == [] and len(copies) == 1


def test_rescore_ignores_existing():
    s, j = _setup()
    canon, repl, copies = _plan_scoring(s, 1, [j], {}, lambda m: None, ignore_existing=True)
    assert [c.id for c in canon] == [j.id] and copies == []
