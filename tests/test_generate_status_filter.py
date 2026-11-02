"""Bulk generate must skip jobs the user has already moved on from.

run_generate selection: a Strong Fit with app_status applied / rejected /
not_pursuing gets no materials (no Opus call) unless force_all_status=True.
interviewing is deliberately NOT skipped — the application is still live.
All LLM / Drive / filesystem seams are stubbed; only the selection logic
and the DB writes are real.
"""
import pytest
from sqlmodel import SQLModel, Session, create_engine

from job_agent.db import Job, Score, User, UserJobMeta, job_id
from job_agent.stages import generate as gen


def _job(company, title="Head of AI"):
    return Job(id=job_id(company, title, "Amsterdam"), source="x", company=company,
               title=title, location="Amsterdam", url="",
               description_raw="x" * 200)


def _score(job, user_id):
    return Score(job_id=job.id, user_id=user_id, tier="Strong Fit", skill_fit=90,
                 preference_alignment=80, recommended_resume_version="ai_focused",
                 gap_assessment="", positioning_notes="", rationale="", raw_response="")


@pytest.fixture()
def engine(tmp_path, monkeypatch):
    eng = create_engine(f"sqlite:///{tmp_path}/jobs.sqlite")
    SQLModel.metadata.create_all(eng)
    monkeypatch.setattr(gen, "init_db", lambda: eng)
    monkeypatch.setattr(gen, "get_session",
                        lambda: Session(eng, expire_on_commit=False))
    monkeypatch.setattr("job_agent.config.load_profile", lambda path: object())
    monkeypatch.setattr("job_agent.config.load_profile_files",
                        lambda profile=None: {"resume_versions/ai_focused.md": "resume"})
    monkeypatch.setattr("job_agent.generation.cover_letter.generate_cover_letter",
                        lambda *a, **k: "letter")
    monkeypatch.setattr("job_agent.generation.resume_recommendation.generate_resume_recommendation",
                        lambda *a, **k: "rec")
    monkeypatch.setattr("job_agent.generation.application_answers.generate_application_answers",
                        lambda *a, **k: "answers")
    monkeypatch.setattr("job_agent.generation.truthfulness.check_grounding",
                        lambda *a, **k: "ok")
    monkeypatch.setattr("job_agent.tailor.budgets_from_markdown", lambda text: None)
    monkeypatch.setattr("job_agent.clustering.pick_canonical",
                        lambda jobs, profile: jobs[0])

    def _write_outputs(job, score, *a, **k):
        out = tmp_path / "outputs" / job.id
        out.mkdir(parents=True, exist_ok=True)
        return out

    monkeypatch.setattr("job_agent.output.write_outputs", _write_outputs)
    return eng


def _seed(eng, specs):
    """specs: list of (company, app_status-or-None). Every job is Strong Fit."""
    with Session(eng) as s:
        user = User(slug="theo", profile_path="profile/theo.yaml")
        s.add(user)
        s.commit()
        s.refresh(user)
        for company, status in specs:
            j = _job(company)
            s.add(j)
            s.add(_score(j, user.id))
            if status:
                s.add(UserJobMeta(job_id=j.id, user_id=user.id, app_status=status))
        s.commit()


def _companies(outputs):
    return {job.company for job, _score, _out in outputs}


MOVED_ON = [("OpenCo", None), ("AppliedCo", "applied"),
            ("RejectedCo", "rejected"), ("ParkedCo", "not_pursuing")]


def test_statused_jobs_are_skipped(engine):
    _seed(engine, MOVED_ON)
    outputs = gen.run_generate(user_slug="theo", echo=lambda m: None)
    assert _companies(outputs) == {"OpenCo"}


def test_force_all_status_includes_statused_jobs(engine):
    _seed(engine, MOVED_ON)
    outputs = gen.run_generate(user_slug="theo", echo=lambda m: None,
                               force_all_status=True)
    assert _companies(outputs) == {"OpenCo", "AppliedCo", "RejectedCo", "ParkedCo"}


def test_interviewing_is_not_skipped(engine):
    _seed(engine, [("LiveCo", "interviewing")])
    outputs = gen.run_generate(user_slug="theo", echo=lambda m: None)
    assert _companies(outputs) == {"LiveCo"}
