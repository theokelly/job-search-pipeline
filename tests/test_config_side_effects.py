"""Phase G: config side effects are explicit, not hidden in readers.

load_profile_files must be a pure disk read; the Drive sync + mirror
refresh happen only via ensure_profile_fresh, called explicitly by
the generation entry points.
"""
from job_agent import config


def test_load_profile_files_does_not_sync(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(config, "_maybe_sync_profile", lambda p: calls.append("sync"))
    monkeypatch.setattr(config, "_maybe_refresh_mirror", lambda p: calls.append("mirror"))
    # Hermetic profile tree (the mirror key only appears when the file exists).
    monkeypatch.chdir(tmp_path)
    (tmp_path / "profile").mkdir()
    (tmp_path / "profile/resume_base.md").write_text("mirror")
    out = config.load_profile_files()
    assert calls == []
    # The generation file keys are always present (missing files -> "").
    assert "narrative_guidance.md" in out
    assert out["resume_base.md"] == "mirror"


def test_ensure_profile_fresh_runs_both_steps(monkeypatch):
    calls = []
    monkeypatch.setattr(config, "_maybe_sync_profile", lambda p: calls.append("sync"))
    monkeypatch.setattr(config, "_maybe_refresh_mirror", lambda p: calls.append("mirror"))
    config.ensure_profile_fresh({"profile_sync": {}})
    assert calls == ["sync", "mirror"]


def test_load_env_reads_dotenv_without_overriding(monkeypatch, tmp_path):
    import os
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("JOB_AGENT_TEST_SENTINEL=from-file\n")
    monkeypatch.delenv("JOB_AGENT_TEST_SENTINEL", raising=False)
    config.load_env()
    assert os.environ["JOB_AGENT_TEST_SENTINEL"] == "from-file"
    # A variable already set wins over the file (override=False).
    (tmp_path / ".env").write_text("JOB_AGENT_TEST_SENTINEL=changed\n")
    config.load_env()
    assert os.environ["JOB_AGENT_TEST_SENTINEL"] == "from-file"
