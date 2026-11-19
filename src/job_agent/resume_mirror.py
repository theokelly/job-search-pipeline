"""The resume mirror: profile/resume_base.md.

The live base resume in Google Drive (tailoring.base_pptx_drive_id) is the
source of truth. This module renders the markdown mirror from it — via
scripts/extract_resume.py — and refreshes the local file when the Drive
doc's modified time no longer matches the stamp recorded in the mirror's
source note.
"""
from __future__ import annotations

import re
import sys
import tempfile
from pathlib import Path

MIRROR_PATH = Path("profile/resume_base.md")
MIRROR_TITLE = "Resume Base"

_STAMP_RE = re.compile(r"\(base modified ([^)]+)\)")


def _load_extractor():
    """Import scripts/extract_resume.py (not part of the installed package)."""
    scripts_dir = Path("scripts").resolve()
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    import extract_resume
    return extract_resume


def mirror_stamp(text: str) -> str | None:
    """The Drive modified time recorded in a mirror's source note."""
    m = _STAMP_RE.search(text)
    return m.group(1) if m else None


def _base_file_id(profile: dict) -> str | None:
    return (profile.get("tailoring") or {}).get("base_pptx_drive_id") or None


def drive_base_modified(profile: dict) -> str:
    """modifiedTime of the Drive base doc (ISO string, as Drive reports it)."""
    from .drive_sync import get_google_services
    drive, _docs = get_google_services(profile)
    meta = drive.files().get(
        fileId=_base_file_id(profile), fields="modifiedTime").execute()
    return meta["modifiedTime"]


def render_current_mirror(profile: dict, modified: str | None = None) -> str:
    """Render the mirror text from the current Drive base.

    `modified` may be passed in to avoid a second metadata call; it is
    embedded in the source note so staleness checks compare like with like.
    """
    from .tailor import download_base_pptx
    if modified is None:
        modified = drive_base_modified(profile)
    extractor = _load_extractor()
    with tempfile.TemporaryDirectory(prefix="resume_mirror_") as td:
        base_path = Path(td) / "base.pptx"
        download_base_pptx(profile, base_path)
        shapes = extractor.load_shapes(base_path)
        return extractor.render(
            base_path, MIRROR_TITLE,
            extractor.extract_main(shapes),
            extractor.render_sidebar(extractor.extract_sidebar(shapes)),
            stamp=modified,
        )


def refresh_mirror_if_stale(profile: dict) -> str:
    """Rewrite profile/resume_base.md when the Drive base has changed.

    Returns "updated", "current", or "skipped" (no Drive base configured,
    or Drive/extraction unavailable). Never raises: generation proceeds
    with whatever mirror is on disk.
    """
    if not _base_file_id(profile):
        return "skipped"
    try:
        modified = drive_base_modified(profile)
        if MIRROR_PATH.exists() and mirror_stamp(MIRROR_PATH.read_text()) == modified:
            return "current"
        MIRROR_PATH.write_text(render_current_mirror(profile, modified))
        return "updated"
    except Exception:
        return "skipped"
