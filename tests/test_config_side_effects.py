"""Phase G: config side effects are explicit, not hidden in readers.

load_profile_files must be a pure disk read; the Drive sync + mirror
refresh happen only via ensure_profile_fresh, called explicitly by
the generation entry points.
"""
from job_agent import config


def test_load_profile_files_does_not_sync(monkeypatch):
    calls = []
    monkeypatch.setattr(config, "_maybe_sync_profile", lambda p: calls.append("sync"))
    monkeypatch.setattr(config, "_maybe_refresh_mirror", lambda p: calls.append("mirror"))
    out = config.load_profile_files()
    assert calls == []
    # The generation file keys are always present (missing files -> "").
    assert "narrative_guidance.md" in out
    assert "resume_base.md" in out


def test_ensure_profile_fresh_runs_both_steps(monkeypatch):
    calls = []
    monkeypatch.setattr(config, "_maybe_sync_profile", lambda p: calls.append("sync"))
    monkeypatch.setattr(config, "_maybe_refresh_mirror", lambda p: calls.append("mirror"))
    config.ensure_profile_fresh({"profile_sync": {}})
    assert calls == ["sync", "mirror"]
