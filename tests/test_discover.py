"""Characterization tests for stages/discover.py run_discover (Phase I-4).

Discovery is orchestration: channel fetchers, the rule pre-filter, and
upserts are patched at the module attributes the lazy imports read, and
the DB is a tmp sqlite (pattern from test_queries.py). The tests pin the
counting contract the CLI summary and cron logs rely on: found / passed /
skipped / dupes / new / updated / errors, per-channel error isolation,
and dry-run saving nothing.
"""
import pytest
from sqlmodel import SQLModel, Session, create_engine, select

from job_agent.db import Job
from job_agent.stages import _common, discover

ECHO = lambda msg: None  # noqa: E731


@pytest.fixture()
def engine(tmp_path, monkeypatch):
    eng = create_engine(f"sqlite:///{tmp_path}/jobs.sqlite")
    SQLModel.metadata.create_all(eng)
    monkeypatch.setattr(discover, "init_db", lambda *a, **k: eng)
    monkeypatch.setattr(discover, "get_session",
                        lambda *a, **k: Session(eng, expire_on_commit=False))
    monkeypatch.setattr(_common, "RUN_LOG_PATH", str(tmp_path / "runs.log"))
    monkeypatch.setattr("job_agent.filter.pre_filter",
                        lambda job, profile: ("Skip" not in job.title,
                                              "title" if "Skip" in job.title else ""))
    monkeypatch.setattr("job_agent.discovery.linkedin.backfill_missing_jds",
                        lambda session: 0)
    return eng


def _profile(monkeypatch, companies, **extra):
    profile = {"target_companies": companies}
    profile.update(extra)
    monkeypatch.setattr("job_agent.config.load_profile", lambda path: profile)
    return profile


def _job(jid, company="Acme", title="Head of AI", url=None, source="greenhouse"):
    return Job(id=jid, source=source, company=company, title=title,
               location="Amsterdam", url=url or f"https://x/{jid}",
               description_raw="jd", status="active")


def _patch_boards(monkeypatch, gh=None, lever=None, ashby=None, workday=None):
    for mod, fn in (("greenhouse", gh), ("lever", lever),
                    ("ashby", ashby), ("workday", workday)):
        if fn is not None:
            monkeypatch.setattr(f"job_agent.discovery.{mod}.fetch_jobs", fn)


ACME = {"name": "Acme", "greenhouse_token": "acme"}
BETA = {"name": "Beta", "lever_slug": "beta"}


def test_boards_accounting_and_save(engine, monkeypatch):
    _profile(monkeypatch, [ACME, BETA, {"name": "NoBoard"}])
    _patch_boards(monkeypatch,
                  gh=lambda name, token: [_job("g1"), _job("g2", title="Skip Me")],
                  lever=lambda name, slug: [_job("l1", company="Beta", source="lever")])
    counts, results = discover.run_discover(echo=ECHO)
    assert counts["found"] == 3 and counts["passed"] == 2
    assert counts["skipped"] == 1 and counts["errors"] == 0
    assert counts["new"] == 2 and counts["updated"] == 0
    assert [a for a, _, _ in results] == ["keep", "skip", "keep"]
    with Session(engine) as s:
        assert {j.id for j in s.exec(select(Job)).all()} == {"g1", "l1"}


def test_board_error_is_isolated(engine, monkeypatch):
    _profile(monkeypatch, [ACME, BETA])

    def boom(name, token):
        raise RuntimeError("board down")

    _patch_boards(monkeypatch, gh=boom,
                  lever=lambda name, slug: [_job("l1", company="Beta", source="lever")])
    counts, _ = discover.run_discover(echo=ECHO)
    assert counts["errors"] == 1
    assert "greenhouse Acme" in counts["error_details"][0]
    assert counts["new"] == 1  # lever still saved


def test_priority_filter_limits_companies(engine, monkeypatch):
    _profile(monkeypatch, [dict(ACME, priority="high"), dict(BETA, priority="low")])
    seen = []
    _patch_boards(monkeypatch,
                  gh=lambda name, token: seen.append(name) or [],
                  lever=lambda name, slug: seen.append(name) or [])
    discover.run_discover(priority="high", echo=ECHO)
    assert seen == ["Acme"]


def test_dry_run_saves_nothing(engine, monkeypatch):
    _profile(monkeypatch, [ACME])
    _patch_boards(monkeypatch, gh=lambda name, token: [_job("g1")])
    counts, _ = discover.run_discover(dry_run=True, echo=ECHO)
    assert counts["passed"] == 1 and counts["new"] == 0 and counts["updated"] == 0
    with Session(engine) as s:
        assert s.exec(select(Job)).all() == []


def test_existing_job_counts_as_updated(engine, monkeypatch):
    _profile(monkeypatch, [ACME])
    _patch_boards(monkeypatch, gh=lambda name, token: [_job("g1")])
    discover.run_discover(echo=ECHO)
    counts, _ = discover.run_discover(echo=ECHO)
    assert counts["new"] == 0 and counts["updated"] == 1


def test_linkedin_without_queries_skips_cleanly(engine, monkeypatch):
    _profile(monkeypatch, [])
    monkeypatch.setattr("job_agent.discovery.linkedin.fetch_jobs",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("called")))
    counts, _ = discover.run_discover(include_linkedin=True, echo=ECHO)
    assert counts["found"] == 0 and counts["errors"] == 0


def test_linkedin_error_does_not_stop_boards(engine, monkeypatch):
    _profile(monkeypatch, [ACME], linkedin_search_queries=["ai lead"])
    monkeypatch.setattr("job_agent.discovery.linkedin.fetch_jobs",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("li down")))
    _patch_boards(monkeypatch, gh=lambda name, token: [_job("g1")])
    counts, _ = discover.run_discover(include_linkedin=True, echo=ECHO)
    assert counts["errors"] == 1 and counts["new"] == 1


def test_email_environment_error_is_a_skip(engine, monkeypatch):
    _profile(monkeypatch, [])

    def no_gmail(**kwargs):
        raise EnvironmentError("gmail not configured")

    monkeypatch.setattr("job_agent.discovery.email_alerts.fetch_alert_jobs", no_gmail)
    counts, _ = discover.run_discover(include_email=True, echo=ECHO)
    assert counts["errors"] == 1
    assert "email (skipped)" in counts["error_details"][0]


def test_fanout_url_claim_dedupes_board_copy(engine, monkeypatch):
    _profile(monkeypatch, [ACME])
    url = "https://job-boards.greenhouse.io/acme/jobs/123"
    stats = {"queries": 1, "results": 1, "candidates": 1, "gated_out": 0,
             "known_skipped": 0, "dead_skipped": 0, "fetched": 1, "gone": 0,
             "extract_failed": 0, "errors": []}
    monkeypatch.setattr("job_agent.discovery.fanout.fetch_jobs",
                        lambda profile, known_urls=None: ([_job("f1", url=url)], stats))
    _patch_boards(monkeypatch,
                  gh=lambda name, token: [_job("g9", title="AI Lead", url=url)])
    counts, _ = discover.run_discover(include_fanout=True, echo=ECHO)
    assert counts["passed"] == 1 and counts["dupes"] == 1
    with Session(engine) as s:
        assert {j.id for j in s.exec(select(Job)).all()} == {"f1"}
