"""Tests for drive_sync.py. The Google APIs are mocked — no network, no credentials."""
import pytest
from job_agent.drive_sync import (
    DriveNotConfigured, _sync_config, _unified_diff, _diffstat,
    convert_doc_to_markdown, fetch_file_text, sync_profile_files,
)

PROFILE = {
    "profile_sync": {
        "credentials_file": "data/drive_token.json",
        "client_secrets_file": "data/drive_client.json",
        "files": [
            {"drive_id": "id-starls", "local_path": "profile/accomplishments/starls.md"},
            {"drive_id": "id-brag", "local_path": "profile/accomplishments/brag_sheet.md"},
        ],
    }
}


def _para(text, style="NORMAL_TEXT", bold=False, italic=False, bullet=None):
    """Build a minimal Docs API paragraph dict."""
    ts = {}
    if bold:
        ts["bold"] = True
    if italic:
        ts["italic"] = True
    p = {
        "paragraphStyle": {"namedStyleType": style},
        "elements": [{"textRun": {"content": text + "\n", "textStyle": ts}}],
    }
    if bullet:
        p["bullet"] = bullet
    return {"paragraph": p}


def _doc(paragraphs, lists=None):
    return {"body": {"content": paragraphs}, "lists": lists or {}}


def test_convert_headings_and_formatting():
    doc = _doc([
        _para("My Title", style="HEADING_1"),
        _para("Intro paragraph."),
        _para("Bold intro", bold=True),
        _para("both", bold=True, italic=True),
    ])
    md = convert_doc_to_markdown(doc)
    assert md.splitlines() == [
        "# My Title", "", "Intro paragraph.", "", "**Bold intro**", "", "***both***",
    ]


def test_convert_bulleted_list():
    lists = {"l1": {"listProperties": {"nestingLevels": [
        {"glyphType": "BULLET_DISC"}, {"glyphType": "BULLET_CIRCLE"},
    ]}}}
    doc = _doc([
        _para("top", bullet={"listId": "l1", "nestingLevel": 0}),
        _para("nested", bullet={"listId": "l1", "nestingLevel": 1}),
        _para("top2", bullet={"listId": "l1", "nestingLevel": 0}),
    ], lists)
    md = convert_doc_to_markdown(doc)
    assert md.splitlines() == ["- top", "  - nested", "- top2"]


def test_convert_numbered_list():
    lists = {"l1": {"listProperties": {"nestingLevels": [{"glyphType": "DECIMAL"}]}}}
    doc = _doc([
        _para("one", bullet={"listId": "l1", "nestingLevel": 0}),
        _para("two", bullet={"listId": "l1", "nestingLevel": 0}),
    ], lists)
    md = convert_doc_to_markdown(doc)
    assert md.splitlines() == ["1. one", "1. two"]


def test_convert_skips_empty_paragraphs():
    doc = _doc([_para(""), _para("real")])
    assert convert_doc_to_markdown(doc) == "real\n"


def test_not_configured():
    with pytest.raises(DriveNotConfigured):
        _sync_config({})


def test_unified_diff():
    d = _unified_diff("f.md", "a\n", "b\n")
    assert "-a" in d and "+b" in d


def test_diffstat():
    added, removed = _diffstat(_unified_diff("f.md", "a\nb\n", "a\nc\nd\n"))
    assert (added, removed) == (2, 1)


class _FakeGet:
    def __init__(self, payload):
        self.payload = payload
    def execute(self):
        return self.payload


class _FakeDriveFiles:
    def __init__(self, metas, blobs):
        self.metas, self.blobs = metas, blobs
    def get(self, fileId, fields=None):
        return _FakeGet(self.metas[fileId])
    def export(self, fileId, mimeType):
        return _FakeGet(self.blobs[fileId])
    def get_media(self, fileId):
        return _FakeGet(self.blobs[fileId])


class _FakeDrive:
    def __init__(self, metas, blobs):
        self._f = _FakeDriveFiles(metas, blobs)
    def files(self):
        return self._f


class _FakeDocs:
    def __init__(self, docs):
        self._docs = docs
    def documents(self):
        return self
    def get(self, documentId):
        return _FakeGet(self._docs[documentId])


def _services():
    metas = {
        "id-starls": {"id": "id-starls", "name": "STARL Examples",
                        "mimeType": "application/vnd.google-apps.document",
                        "modifiedTime": "2026-05-29T02:37:20.148Z"},
        "id-brag": {"id": "id-brag", "name": "Brag Sheet",
                      "mimeType": "text/markdown",
                      "modifiedTime": "2026-05-06T00:00:00Z"},
    }
    docs = {
        "id-starls": _doc([
            _para("STARLs", style="HEADING_1"),
            _para("Story one", bullet={"listId": "l1", "nestingLevel": 0}),
        ], {"l1": {"listProperties": {"nestingLevels": [{"glyphType": "BULLET_DISC"}]}}}),
    }
    blobs = {"id-brag": b"# Brag\n\nsame"}
    return _FakeDrive(metas, blobs), _FakeDocs(docs)


def test_fetch_google_doc_converts_to_markdown():
    drive, docs = _services()
    name, modified, text = fetch_file_text(drive, docs, "id-starls")
    assert name == "STARL Examples"
    assert text.splitlines() == ["# STARLs", "", "- Story one"]


def test_fetch_raw_file_downloads():
    drive, docs = _services()
    name, modified, text = fetch_file_text(drive, docs, "id-brag")
    assert text == "# Brag\n\nsame"


def _patch_services(monkeypatch):
    drive, docs = _services()
    monkeypatch.setattr("job_agent.drive_sync.get_google_services",
                        lambda profile: (drive, docs))


def test_sync_dry_run_reports_diff(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    _patch_services(monkeypatch)
    local = tmp_path / "profile/accomplishments"
    local.mkdir(parents=True)
    (local / "starls.md").write_text("# STARLs\n\n- Old story")
    (local / "brag_sheet.md").write_text("# Brag\n\nsame")
    results = sync_profile_files(PROFILE, dry_run=True)
    by_path = {r.local_path: r for r in results}
    starls = by_path["profile/accomplishments/starls.md"]
    assert starls.status == "would-update"
    assert (starls.added, starls.removed) == (1, 1)
    assert by_path["profile/accomplishments/brag_sheet.md"].status == "in-sync"
    assert (local / "starls.md").read_text() == "# STARLs\n\n- Old story"


def test_sync_apply_writes(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    _patch_services(monkeypatch)
    local = tmp_path / "profile/accomplishments"
    local.mkdir(parents=True)
    (local / "starls.md").write_text("old")
    (local / "brag_sheet.md").write_text("# Brag\n\nsame")
    results = sync_profile_files(PROFILE, dry_run=False)
    by_path = {r.local_path: r for r in results}
    assert by_path["profile/accomplishments/starls.md"].status == "updated"
    assert (local / "starls.md").read_text().splitlines() == ["# STARLs", "", "- Story one"]


def test_sync_missing_local(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    _patch_services(monkeypatch)
    (tmp_path / "profile/accomplishments").mkdir(parents=True)
    results = sync_profile_files(PROFILE, dry_run=True)
    assert all(r.status == "missing-local" for r in results)
    results = sync_profile_files(PROFILE, dry_run=False)
    assert all(r.status == "updated" for r in results)
    assert (tmp_path / "profile/accomplishments/starls.md").exists()


def test_sync_drive_error_reported(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    class _Broken:
        def files(self):
            raise RuntimeError("boom")
    monkeypatch.setattr("job_agent.drive_sync.get_google_services",
                        lambda profile: (_Broken(), _Broken()))
    results = sync_profile_files(PROFILE, dry_run=True)
    assert all(r.status == "error" and "boom" in r.error for r in results)


def test_sync_only_filter(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    _patch_services(monkeypatch)
    (tmp_path / "profile/accomplishments").mkdir(parents=True)
    results = sync_profile_files(PROFILE, dry_run=True, only="brag_sheet")
    assert len(results) == 1
    assert results[0].local_path == "profile/accomplishments/brag_sheet.md"
    with pytest.raises(Exception, match="No mapped file matches"):
        sync_profile_files(PROFILE, dry_run=True, only="nonexistent")


def _profile_with_sync(sync_on_load):
    p = dict(PROFILE)
    p["profile_sync"] = dict(PROFILE["profile_sync"])
    p["profile_sync"]["sync_on_load"] = sync_on_load
    return p


def test_maybe_sync_runs_when_enabled(monkeypatch, tmp_path, capsys):
    import job_agent.config as config
    monkeypatch.chdir(tmp_path)
    _patch_services(monkeypatch)
    local = tmp_path / "profile/accomplishments"
    local.mkdir(parents=True)
    (local / "starls.md").write_text("old")
    (local / "brag_sheet.md").write_text("# Brag\n\nsame")
    files = config.load_profile_files(
        ["profile/accomplishments/starls.md"],
        profile=_profile_with_sync(True),
    )
    # sync ran and updated the stale file before loading
    assert files["accomplishments/starls.md"].splitlines()[0] == "# STARLs"
    assert "synced from Drive" in capsys.readouterr().err


def test_maybe_sync_skipped_when_disabled(monkeypatch, tmp_path):
    import job_agent.config as config
    monkeypatch.chdir(tmp_path)
    _patch_services(monkeypatch)
    local = tmp_path / "profile/accomplishments"
    local.mkdir(parents=True)
    (local / "starls.md").write_text("old")
    files = config.load_profile_files(
        ["profile/accomplishments/starls.md"],
        profile=_profile_with_sync(False),
    )
    assert files["accomplishments/starls.md"] == "old"


def test_maybe_sync_failure_falls_back_to_local(monkeypatch, tmp_path, capsys):
    import job_agent.config as config
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("job_agent.drive_sync.sync_profile_files",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("nope")))
    local = tmp_path / "profile/accomplishments"
    local.mkdir(parents=True)
    (local / "starls.md").write_text("old")
    files = config.load_profile_files(
        ["profile/accomplishments/starls.md"],
        profile=_profile_with_sync(True),
    )
    assert files["accomplishments/starls.md"] == "old"
    assert "auto-sync failed" in capsys.readouterr().err


def test_staleness_guard_skips_recent_sync(monkeypatch, tmp_path):
    import time
    import job_agent.config as config
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    (tmp_path / "data/.drive_sync_last").write_text(str(time.time()))
    (tmp_path / "profile/accomplishments").mkdir(parents=True)
    (tmp_path / "profile/accomplishments/starls.md").write_text("old")
    called = []
    monkeypatch.setattr("job_agent.drive_sync.sync_profile_files",
                        lambda *a, **k: called.append(1) or [])
    config.load_profile_files(
        ["profile/accomplishments/starls.md"],
        profile=_profile_with_sync(True),
    )
    assert called == []  # skipped: synced <15 min ago


def test_staleness_guard_runs_when_stale(monkeypatch, tmp_path):
    import time
    import job_agent.config as config
    monkeypatch.chdir(tmp_path)
    _patch_services(monkeypatch)
    (tmp_path / "data").mkdir()
    (tmp_path / "data/.drive_sync_last").write_text(str(time.time() - 3600))
    (tmp_path / "profile/accomplishments").mkdir(parents=True)
    (tmp_path / "profile/accomplishments/starls.md").write_text("old")
    (tmp_path / "profile/accomplishments/brag_sheet.md").write_text("# Brag\n\nsame")
    config.load_profile_files(
        ["profile/accomplishments/starls.md"],
        profile=_profile_with_sync(True),
    )
    # stamp refreshed by the sync
    assert float((tmp_path / "data/.drive_sync_last").read_text()) > time.time() - 60
