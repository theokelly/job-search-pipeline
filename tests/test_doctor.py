import os
import pytest
from job_agent import resume_mirror
from job_agent.doctor import (
    Report, check_env, check_profile, check_resume_sync, run_doctor,
)


def test_report_has_errors():
    r = Report()
    assert not r.has_errors
    r.add("warning", "x", "y")
    assert not r.has_errors
    r.add("error", "x", "y")
    assert r.has_errors


def test_check_env_missing_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    r = Report()
    check_env(r)
    assert any(i.level == "error" and "ANTHROPIC_API_KEY" in i.message for i in r.issues)


def test_check_env_key_present(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    r = Report()
    check_env(r)
    assert not r.has_errors


def _write_profile(tmp_path, content):
    (tmp_path / "profile").mkdir(parents=True)
    (tmp_path / "profile" / "theo.yaml").write_text(content)
    (tmp_path / "profile" / "resume_base.md").write_text("# Resume Base\n")


def test_check_profile_valid(monkeypatch, tmp_path):
    _write_profile(tmp_path, "geography:\n  target_city: amsterdam\n")
    monkeypatch.chdir(tmp_path)
    r = Report()
    assert check_profile(r) is not None
    assert not r.has_errors


def test_check_profile_invalid(monkeypatch, tmp_path):
    _write_profile(tmp_path, "pipeline:\n  expire_after_days: soon\n")
    monkeypatch.chdir(tmp_path)
    r = Report()
    assert check_profile(r) is None
    assert r.has_errors


def _profile_with_base():
    return {"tailoring": {"base_pptx_drive_id": "base-1"}}


def test_check_resume_sync_missing_mirror(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    r = Report()
    check_resume_sync(r, _profile_with_base())
    assert any(i.level == "error" and "resume_base.md" in i.message for i in r.issues)


def test_check_resume_sync_unconfigured(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "profile").mkdir()
    (tmp_path / "profile" / "resume_base.md").write_text("# Resume Base\n")
    r = Report()
    check_resume_sync(r, {})
    assert not r.has_errors
    assert any(i.level == "warning" and "cannot verify" in i.message for i in r.issues)


def test_check_resume_sync_in_sync_and_stale(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "profile").mkdir()
    mirror = tmp_path / "profile" / "resume_base.md"
    mirror.write_text("# Resume Base\nbody\n")
    monkeypatch.setattr(resume_mirror, "render_current_mirror",
                        lambda p: "# Resume Base\nbody\n")
    r = Report()
    check_resume_sync(r, _profile_with_base())
    assert not r.has_errors
    monkeypatch.setattr(resume_mirror, "render_current_mirror",
                        lambda p: "# Resume Base\nchanged\n")
    r = Report()
    check_resume_sync(r, _profile_with_base())
    assert r.has_errors


def test_run_doctor_healthy(monkeypatch, tmp_path, capsys):
    _write_profile(tmp_path, "geography:\n  target_city: amsterdam\n")
    (tmp_path / "data").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    # no generation files -> warnings only; mirror present but no Drive base
    # configured -> the sync check warns rather than errors
    code = run_doctor()
    assert code == 0
    out = capsys.readouterr().out
    assert "job-agent doctor" in out


def test_run_doctor_errors(monkeypatch, tmp_path, capsys):
    # no profile at all + no API key -> errors
    (tmp_path / "profile").mkdir(parents=True)
    (tmp_path / "data").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    code = run_doctor()
    assert code == 1
