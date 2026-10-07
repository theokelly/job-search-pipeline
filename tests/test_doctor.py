import os
import pytest
from job_agent.doctor import (
    Report, _parse_source_pptx, _md_title,
    check_env, check_profile, run_doctor,
)


def test_parse_source_pptx():
    md = "> Source: `TheoKelly_CV_Amsterdam.pptx` (in this folder; synced 2026-10-06). blah"
    assert _parse_source_pptx(md) == "TheoKelly_CV_Amsterdam.pptx"
    assert _parse_source_pptx("# Just a title\n\nno source note") is None


def test_md_title():
    assert _md_title("# Theo Kelly\n\nbody") == "Theo Kelly"
    assert _md_title("no h1 here") is None


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
    pdir = tmp_path / "profile" / "resume_versions"
    pdir.mkdir(parents=True)
    (tmp_path / "profile" / "theo.yaml").write_text(content)


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


def test_run_doctor_healthy(monkeypatch, tmp_path, capsys):
    _write_profile(tmp_path, "geography:\n  target_city: amsterdam\n")
    (tmp_path / "data").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    # no generation files -> warnings only; no pptx -> info only
    code = run_doctor()
    assert code == 0
    out = capsys.readouterr().out
    assert "job-agent doctor" in out


def test_run_doctor_errors(monkeypatch, tmp_path, capsys):
    # no profile at all + no API key -> errors
    (tmp_path / "profile" / "resume_versions").mkdir(parents=True)
    (tmp_path / "data").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    code = run_doctor()
    assert code == 1
