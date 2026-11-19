"""Tests for the resume mirror (profile/resume_base.md, generated from the
live Drive base). Drive is faked — no network, no credentials."""
import pytest
from job_agent import resume_mirror

STAMP = "2026-11-19T10:00:00.000Z"


def _mirror_text(stamp):
    return (
        "# Resume Base\n\n"
        f"> Source: live base resume in Google Drive (base modified {stamp}). x\n"
    )


def test_mirror_stamp():
    assert resume_mirror.mirror_stamp(_mirror_text(STAMP)) == STAMP
    assert resume_mirror.mirror_stamp("# Resume Base\nno stamp here\n") is None


def test_refresh_skipped_without_base_id():
    assert resume_mirror.refresh_mirror_if_stale({}) == "skipped"
    assert resume_mirror.refresh_mirror_if_stale({"tailoring": {}}) == "skipped"


def _profile():
    return {"tailoring": {"base_pptx_drive_id": "base-1"}}


def test_refresh_updates_when_stamp_differs(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "profile").mkdir()
    mirror = tmp_path / "profile" / "resume_base.md"
    mirror.write_text("# Resume Base\nold\n")
    monkeypatch.setattr(resume_mirror, "drive_base_modified", lambda p: STAMP)
    monkeypatch.setattr(resume_mirror, "render_current_mirror",
                        lambda p, modified=None: "# Resume Base\nnew\n")
    assert resume_mirror.refresh_mirror_if_stale(_profile()) == "updated"
    assert mirror.read_text() == "# Resume Base\nnew\n"


def test_refresh_current_when_stamp_matches(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "profile").mkdir()
    mirror = tmp_path / "profile" / "resume_base.md"
    mirror.write_text(_mirror_text(STAMP))
    monkeypatch.setattr(resume_mirror, "drive_base_modified", lambda p: STAMP)

    def _boom(p, modified=None):
        raise AssertionError("should not render when the stamp already matches")

    monkeypatch.setattr(resume_mirror, "render_current_mirror", _boom)
    assert resume_mirror.refresh_mirror_if_stale(_profile()) == "current"
    assert mirror.read_text() == _mirror_text(STAMP)


def test_refresh_skipped_when_drive_unavailable(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "profile").mkdir()
    (tmp_path / "profile" / "resume_base.md").write_text("# Resume Base\nold\n")
    monkeypatch.setattr(resume_mirror, "drive_base_modified",
                        lambda p: (_ for _ in ()).throw(RuntimeError("no drive")))
    assert resume_mirror.refresh_mirror_if_stale(_profile()) == "skipped"
