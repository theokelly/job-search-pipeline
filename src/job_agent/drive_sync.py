"""Google Drive sync for profile source files (Phase C1).

Maps Drive file IDs to local paths via the `profile_sync:` section of
theo.yaml::

    profile_sync:
      credentials_file: "data/drive_token.json"      # OAuth token (created by drive-auth)
      client_secrets_file: "data/drive_client.json"  # OAuth client secrets from Google Cloud
      files:
        - drive_id: "1abc..."
          local_path: "profile/accomplishments/starls.md"

Manual trigger, dry-run/diff first — never silently overwrites local files.
Also supports auto-sync before generation loads (see sync_on_load in
config.py's _maybe_sync_profile).
"""
from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from pathlib import Path


class DriveSyncError(Exception):
    """Base error for Drive sync failures."""


class DriveAuthError(DriveSyncError):
    """Raised when Drive credentials are missing/invalid, with setup help."""


class DriveNotConfigured(DriveSyncError):
    """Raised when theo.yaml has no profile_sync section."""


SCOPES = [
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/documents.readonly",
]


@dataclass
class SyncResult:
    local_path: str
    drive_id: str
    drive_name: str = ""
    drive_modified: str = ""
    status: str = ""  # in-sync | would-update | updated | missing-local | error
    diff: str = ""
    error: str = ""
    added: int = 0      # diffstat: lines added vs local
    removed: int = 0    # diffstat: lines removed vs local


def _sync_config(profile: dict) -> dict:
    cfg = profile.get("profile_sync")
    if not cfg:
        raise DriveNotConfigured(
            "No 'profile_sync' section in theo.yaml. Add it with credentials_file "
            "and the files list (see drive_sync.py docstring)."
        )
    return cfg


def load_credentials(credentials_file: str):
    """Load stored OAuth credentials, refreshing when expired.
    Returns None when no usable token exists (missing, expired without
    refresh token, or lacking the current SCOPES)."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    path = Path(credentials_file)
    if not path.exists():
        return None
    creds = Credentials.from_authorized_user_file(str(path), SCOPES)
    if not creds or set(creds.scopes or []) < set(SCOPES):
        return None  # re-auth needed for new scopes
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        path.write_text(creds.to_json())
    return creds if creds.valid else None


def get_google_services(profile: dict):
    """Build authenticated Drive + Docs services. Raises DriveAuthError with
    setup instructions when credentials are missing or invalid."""
    from googleapiclient.discovery import build

    cfg = _sync_config(profile)
    creds = load_credentials(cfg["credentials_file"])
    if not creds:
        raise DriveAuthError(
            f"No valid Drive credentials at {cfg['credentials_file']}. "
            "Run scripts/drive_auth_local.py on your Mac to reconnect."
        )
    return (build("drive", "v3", credentials=creds),
            build("docs", "v1", credentials=creds))


# ---------------------------------------------------------------------------
# Google Doc -> Markdown conversion (via the Docs API, preserves structure)
# ---------------------------------------------------------------------------
_HEADINGS = {
    "TITLE": "#", "SUBTITLE": "##",
    "HEADING_1": "#", "HEADING_2": "##", "HEADING_3": "###",
    "HEADING_4": "####", "HEADING_5": "#####", "HEADING_6": "######",
}


def _render_runs(elements: list) -> str:
    """Render a paragraph's textRun elements to markdown inline formatting."""
    parts: list[str] = []
    for elem in elements:
        tr = elem.get("textRun")
        if not tr:
            continue
        content = tr.get("content", "")
        if content == "\n":
            continue  # paragraph terminator, not content
        ts = tr.get("textStyle", {})
        stripped = content.strip()
        if not stripped:
            parts.append(content)
            continue
        # Keep surrounding whitespace outside the markers: ** bold ** is broken.
        prefix = content[: len(content) - len(content.lstrip())]
        suffix = content[len(content.rstrip()):]
        inner = stripped
        if ts.get("bold") and ts.get("italic"):
            inner = f"***{inner}***"
        elif ts.get("bold"):
            inner = f"**{inner}**"
        elif ts.get("italic"):
            inner = f"*{inner}*"
        if ts.get("strikethrough"):
            inner = f"~~{inner}~~"
        link = (ts.get("link") or {}).get("url")
        if link:
            inner = f"[{inner}]({link})"
        parts.append(f"{prefix}{inner}{suffix}")
    return "".join(parts).strip()


def convert_doc_to_markdown(doc: dict) -> str:
    """Convert a Docs API document dict to markdown.

    Handles headings, bold/italic/strikethrough, links, and bulleted +
    numbered lists (with nesting). Tables and other block types are skipped.
    """
    lists_meta = doc.get("lists", {})
    blocks: list[tuple[str, str]] = []  # (kind, markdown) — kind guides spacing
    for el in doc.get("body", {}).get("content", []):
        para = el.get("paragraph")
        if not para:
            continue
        text = _render_runs(para.get("elements", []))
        if not text:
            continue
        bullet = para.get("bullet")
        if bullet:
            level = bullet.get("nestingLevel", 0)
            glyph = ""
            try:
                glyph = lists_meta[bullet["listId"]]["listProperties"] \
                    ["nestingLevels"][level]["glyphType"]
            except (KeyError, IndexError):
                pass
            indent = "  " * level
            marker = "-" if "BULLET" in glyph else "1."
            blocks.append(("list", f"{indent}{marker} {text}"))
            continue
        style = para.get("paragraphStyle", {}).get("namedStyleType", "NORMAL_TEXT")
        if style in _HEADINGS:
            blocks.append(("heading", f"{_HEADINGS[style]} {text}"))
        else:
            blocks.append(("para", text))

    # Join: blank line between blocks, except consecutive list items stay tight.
    lines: list[str] = []
    prev_kind = ""
    for kind, md in blocks:
        if lines and not (kind == "list" and prev_kind == "list"):
            lines.append("")
        lines.append(md)
        prev_kind = kind
    return "\n".join(lines).rstrip("\n") + "\n"


def fetch_file_text(drive_service, docs_service, drive_id: str) -> tuple[str, str, str]:
    """Return (name, modified_time, text) for a Drive file.

    Google Docs go through the Docs API and are converted to markdown
    (headings, bold/italic, lists preserved). Sheets export as CSV.
    Everything else downloads raw.
    """
    meta = drive_service.files().get(
        fileId=drive_id, fields="id, name, mimeType, modifiedTime"
    ).execute()
    mime = meta.get("mimeType", "")
    if mime == "application/vnd.google-apps.document":
        doc = docs_service.documents().get(documentId=drive_id).execute()
        text = convert_doc_to_markdown(doc)
    elif mime == "application/vnd.google-apps.spreadsheet":
        data = drive_service.files().export(fileId=drive_id, mimeType="text/csv").execute()
        text = data.decode("utf-8") if isinstance(data, bytes) else data
    else:
        data = drive_service.files().get_media(fileId=drive_id).execute()
        text = data.decode("utf-8") if isinstance(data, bytes) else data
    return meta.get("name", drive_id), meta.get("modifiedTime", ""), text


def _unified_diff(local_path: str, old: str, new: str) -> str:
    return "\n".join(difflib.unified_diff(
        old.splitlines(), new.splitlines(),
        f"local:{local_path}", "drive", lineterm="",
    ))


def _diffstat(diff: str) -> tuple[int, int]:
    """Count added/removed lines in a unified diff (skips ---/+++ headers)."""
    added = removed = 0
    for line in diff.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            added += 1
        elif line.startswith("-"):
            removed += 1
    return added, removed


def sync_profile_files(profile: dict, dry_run: bool = True,
                       only: str | None = None) -> list[SyncResult]:
    """Compare each mapped Drive file against its local copy.

    dry_run=True (default): report diffs, change nothing.
    dry_run=False: write Drive content to local files that differ.
    only: if given, only process entries whose local_path contains it.
    """
    cfg = _sync_config(profile)
    drive_service, docs_service = get_google_services(profile)
    results: list[SyncResult] = []
    entries = cfg.get("files") or []
    if only:
        entries = [e for e in entries if only in e.get("local_path", "")]
        if not entries:
            raise DriveSyncError(f"No mapped file matches '{only}'.")
    for entry in entries:
        drive_id = entry["drive_id"]
        local_path = entry["local_path"]
        res = SyncResult(local_path=local_path, drive_id=drive_id)
        try:
            name, modified, drive_text = fetch_file_text(drive_service, docs_service, drive_id)
        except Exception as e:
            res.status, res.error = "error", str(e)
            results.append(res)
            continue
        res.drive_name, res.drive_modified = name, modified
        local = Path(local_path)
        if not local.exists():
            res.status = "missing-local"
            res.diff = f"(local file does not exist; Drive has {len(drive_text)} chars)"
            if not dry_run:
                local.parent.mkdir(parents=True, exist_ok=True)
                local.write_text(drive_text)
                res.status = "updated"
            results.append(res)
            continue
        local_text = local.read_text()
        if local_text == drive_text:
            res.status = "in-sync"
        else:
            res.diff = _unified_diff(local_path, local_text, drive_text)
            res.added, res.removed = _diffstat(res.diff)
            if dry_run:
                res.status = "would-update"
            else:
                local.write_text(drive_text)
                res.status = "updated"
        results.append(res)
    return results
