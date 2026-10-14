"""Resume tailoring: apply agent-generated edits to a PPTX copy.

Reads resume_recommendation.md from an application output directory, copies
the base resume PPTX, and applies replace/reorder edits with formatting
preserved. Flags anything that might have overflowed for a quick visual
check. A human review pass over the tailored PPTX is still expected — this
removes the mechanical toil, not the judgment.
"""

from __future__ import annotations

import difflib
import re
from pathlib import Path

from pptx import Presentation

MOVE_FIRST = "__FIRST__"
MATCH_THRESHOLD = 0.8
OVERFLOW_HEADROOM = 1.1  # flag a text frame that grows more than 10%


# ---------------------------------------------------------------------------
# Recommendation parsing
# ---------------------------------------------------------------------------

_LABEL_FIELDS = {
    "Original": "original",
    "Suggested": "suggested",
    "Move": "original",
    "After": "suggested",
    "Why": "reason",
}

_VERSION_RE = re.compile(r"\*\*([^*]+?):\*\*\s*(.+)")
_HEADER_RE = re.compile(r"\*\*(.+?)\*\*")
_ITEM_RE = re.compile(r"-\s+\*\*(.+?)\*\*:?\s?(.*)")


def parse_recommendation(md_text: str) -> tuple[str | None, list[dict]]:
    """Parse resume_recommendation.md into (version, edits).

    Each edit: {operation, section, original, suggested, reason}.
    operation is 'replace' or 'reorder' (Move/After labels).
    """
    version: str | None = None
    edits: list[dict] = []
    current: dict | None = None
    field: str | None = None

    for raw in md_text.split("\n"):
        stripped = raw.strip()

        m = _VERSION_RE.fullmatch(stripped)
        if m and m.group(1).strip().lower() == "resume version":
            version = m.group(2).strip()
            field = None
            continue

        m = _HEADER_RE.fullmatch(stripped)
        if m:
            inner = m.group(1).strip()
            if inner.endswith(":"):
                field = None
                continue
            current = {"operation": "replace", "section": inner,
                       "original": "", "suggested": "", "reason": ""}
            edits.append(current)
            field = None
            continue

        m = _ITEM_RE.match(stripped)
        if m and current is not None:
            label, value = m.group(1).strip().rstrip(":"), m.group(2).strip()
            key = _LABEL_FIELDS.get(label)
            if label in ("Move", "After"):
                current["operation"] = "reorder"
            if key:
                current[key] = value
                field = key
            else:
                field = None
            continue

        if current is not None and field:
            if stripped == "":
                current[field] += "\n"
            elif raw.startswith(("  ", "\t")):
                sep = "" if current[field].endswith("\n") else " "
                current[field] += sep + stripped
            else:
                field = None
            continue

    for e in edits:
        for k in ("original", "suggested", "reason"):
            e[k] = e[k].strip()
        if e["suggested"] == MOVE_FIRST and e["operation"] != "reorder":
            # The tool schema documents suggested='__FIRST__' as "move to the
            # top of its section"; models often emit it under a replace op —
            # honor the intent as a reorder instead of writing it literally.
            e["operation"] = "reorder"
    edits = [e for e in edits if e["suggested"] or e["original"]]
    return version, edits


# ---------------------------------------------------------------------------
# Layout budgets (from the resume markdown the LLM saw)
# ---------------------------------------------------------------------------

def budgets_from_markdown(resume_text: str) -> list[dict]:
    """Character budgets per editable text, derived from the resume markdown.

    Budget = current character count: a replacement within budget cannot
overflow its text frame, because the original fits. Reorders need no
    budget since the text content is unchanged.
    """
    budgets: list[dict] = []
    section: str | None = None
    company: str | None = None
    counters: dict[str, int] = {}
    for raw in resume_text.split("\n"):
        s = raw.strip()
        if s.startswith("## "):
            section = s[3:].strip()
            company = None
        elif s.startswith("### ") and section in ("Work Experience", "Early Career"):
            company = s[4:].split("|")[0].strip()
        elif section == "Introduction" and s and not s.startswith(("#", "-", ">", "*")):
            budgets.append({"locator": "Introduction", "original": s, "budget": len(s)})
        elif section in ("Work Experience", "Early Career") and s.startswith("- "):
            bullet = s[2:].strip()
            where = f"{section} | {company}" if company else section
            counters[where] = counters.get(where, 0) + 1
            budgets.append({"locator": f"{where} bullet {counters[where]}",
                            "original": bullet, "budget": len(bullet)})
    return budgets


# ---------------------------------------------------------------------------
# PPTX editing
# ---------------------------------------------------------------------------

def _norm(text: str | None) -> str:
    return re.sub(r"\s+", " ", text or "").strip().lower()


def _iter_paragraphs(prs):
    for slide in prs.slides:
        for shape in slide.shapes:
            if not shape.has_text_frame:
                continue
            for para in shape.text_frame.paragraphs:
                yield shape, para


def _similarity(a: str, b: str) -> float:
    if a in b or b in a:
        return 0.95
    return difflib.SequenceMatcher(None, a, b).ratio()


def _replace_paragraph_text(para, new_text: str) -> None:
    """Replace paragraph text, keeping the first run's font formatting."""
    font = None
    for run in para.runs:
        if run.text.strip():
            font = run.font
            break
    para.text = new_text
    if font is None or not para.runs:
        return
    new_font = para.runs[0].font
    try:
        if font.size is not None:
            new_font.size = font.size
        if font.bold is not None:
            new_font.bold = font.bold
        if font.italic is not None:
            new_font.italic = font.italic
        if font.name is not None:
            new_font.name = font.name
        rgb = font.color.rgb
        if rgb is not None:
            new_font.color.rgb = rgb
    except Exception:
        pass


def _move_paragraph_after(para, para_after, text_frame) -> None:
    """Move para to directly after para_after (or first if para_after is None)."""
    txBody = text_frame._txBody
    p_el = para._p
    txBody.remove(p_el)
    if para_after is None:
        first_p = next((el for el in txBody if el.tag.endswith("}p")), None)
        if first_p is not None:
            first_p.addprevious(p_el)
        else:
            txBody.append(p_el)
    else:
        para_after._p.addnext(p_el)


def _apply_replace(prs, edit: dict) -> dict:
    original = edit.get("original", "").strip()
    suggested = edit.get("suggested", "").strip()
    if not suggested:
        return {"status": "skipped", "detail": "empty suggested text"}
    if not original:
        return {"status": "skipped",
                "detail": "no original text to match; add the exact bullet text as Original"}
    target = _norm(original)
    best = None
    best_ratio = 0.0
    for shape, para in _iter_paragraphs(prs):
        ptext = _norm(para.text)
        if not ptext:
            continue
        ratio = _similarity(target, ptext)
        if ratio > best_ratio:
            best_ratio = ratio
            best = (shape, para)
    if best is None or best_ratio < MATCH_THRESHOLD:
        return {"status": "skipped",
                "detail": f"could not find matching bullet (best similarity {best_ratio:.2f})"}
    _, para = best
    old_len = len(para.text or "")
    _replace_paragraph_text(para, suggested)
    result = {"status": "applied",
              "detail": f"replaced ({old_len} -> {len(suggested)} chars, match {best_ratio:.2f})"}
    if len(suggested) > old_len * OVERFLOW_HEADROOM:
        result["status"] = "flagged"
        result["detail"] += "; longer than original — verify layout"
    return result


def _apply_reorder(prs, edit: dict) -> dict:
    original = edit.get("original", "").strip()
    after_text = edit.get("suggested", "").strip()
    if not original:
        return {"status": "skipped", "detail": "no bullet text to move"}
    target = _norm(original)
    best = None
    best_ratio = 0.0
    for shape, para in _iter_paragraphs(prs):
        ptext = _norm(para.text)
        if not ptext:
            continue
        ratio = _similarity(target, ptext)
        if ratio > best_ratio:
            best_ratio = ratio
            best = (shape, para)
    if best is None or best_ratio < MATCH_THRESHOLD:
        return {"status": "skipped",
                "detail": f"could not find bullet to move (best {best_ratio:.2f})"}
    shape, para = best
    tf = shape.text_frame
    if after_text == MOVE_FIRST or not after_text:
        _move_paragraph_after(para, None, tf)
        return {"status": "applied",
                "detail": f"moved to top of its section (match {best_ratio:.2f})"}
    anchor_target = _norm(after_text)
    anchor = None
    anchor_ratio = 0.0
    for p2 in tf.paragraphs:
        if p2 is para:
            continue
        ptext = _norm(p2.text)
        if not ptext:
            continue
        r2 = _similarity(anchor_target, ptext)
        if r2 > anchor_ratio:
            anchor_ratio = r2
            anchor = p2
    if anchor is None or anchor_ratio < MATCH_THRESHOLD:
        return {"status": "skipped",
                "detail": "anchor bullet not found in the same section; bullet left in place"}
    _move_paragraph_after(para, anchor, tf)
    return {"status": "applied",
            "detail": f"moved after anchor bullet (match {best_ratio:.2f}/{anchor_ratio:.2f})"}


def apply_edits(pptx_path: Path | str, edits: list[dict],
                output_path: Path | str, dry_run: bool = False) -> dict:
    """Apply edits to a copy of the PPTX. Returns a report dict."""
    pptx_path = Path(pptx_path)
    output_path = Path(output_path)
    prs = Presentation(str(pptx_path))

    def frame_counts():
        counts: dict[int, int] = {}
        for shape, para in _iter_paragraphs(prs):
            counts.setdefault(id(shape), 0)
            counts[id(shape)] += len(para.text or "")
        return counts

    before = frame_counts()
    applied, skipped, flagged = [], [], []

    for edit in edits:
        op = edit.get("operation", "replace")
        if op == "reorder":
            result = _apply_reorder(prs, edit)
        else:
            result = _apply_replace(prs, edit)
        result["section"] = edit.get("section", "")
        result["operation"] = op
        if result["status"] == "applied":
            applied.append(result)
        elif result["status"] == "flagged":
            flagged.append(result)
        else:
            skipped.append(result)

    after = frame_counts()
    for shape_id, after_n in after.items():
        before_n = before.get(shape_id, 0)
        if before_n and after_n > before_n * OVERFLOW_HEADROOM:
            flagged.append({
                "section": "", "operation": "overflow-check", "status": "flagged",
                "detail": f"a text frame grew {before_n} -> {after_n} chars; eyeball it",
            })

    if not dry_run:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        prs.save(str(output_path))

    return {
        "output_path": str(output_path),
        "applied": applied,
        "skipped": skipped,
        "flagged": flagged,
        "dry_run": dry_run,
    }


# ---------------------------------------------------------------------------
# Google Drive: base download + tailored upload
# ---------------------------------------------------------------------------

def _tailoring_cfg(profile: dict) -> dict:
    return profile.get("tailoring") or {}


def download_base_pptx(profile: dict, dest: Path) -> Path:
    """Download the static base resume as PPTX (tailoring.base_pptx_drive_id).

    Handles both native .pptx files and Google Slides docs (exported as PPTX).
    """
    from .drive_sync import get_google_services
    from googleapiclient.http import MediaIoBaseDownload

    file_id = _tailoring_cfg(profile).get("base_pptx_drive_id")
    if not file_id:
        raise ValueError("tailoring.base_pptx_drive_id is not set in theo.yaml")
    drive, _ = get_google_services(profile)
    meta = drive.files().get(fileId=file_id, fields="mimeType,name").execute()
    if meta.get("mimeType") == "application/vnd.google-apps.presentation":
        request = drive.files().export_media(
            fileId=file_id,
            mimeType="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        )
    else:
        request = drive.files().get_media(fileId=file_id)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(dest, "wb") as f:
        downloader = MediaIoBaseDownload(f, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
    return dest


def _find_or_create_folder(drive, name: str, parent_id: str) -> str:
    """Return the Drive folder id for `name` under parent, creating it if needed."""
    q = (f"name='{name}' and '{parent_id}' in parents "
         f"and mimeType='application/vnd.google-apps.folder' and trashed=false")
    resp = drive.files().list(q=q, fields="files(id, name)", pageSize=10).execute()
    files = resp.get("files", [])
    if files:
        return files[0]["id"]
    created = drive.files().create(
        body={"name": name, "mimeType": "application/vnd.google-apps.folder",
              "parents": [parent_id]},
        fields="id",
    ).execute()
    return created["id"]


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:40]


def upload_tailored_pptx(profile: dict, local_path: Path, output_dir: Path) -> dict:
    """Upload the tailored PPTX to the current YYYY_MM folder in Drive.

    Returns {file_id, web_link, folder_id}. Company/date for the filename
    come from score.json in the output dir when available.
    """
    from .drive_sync import get_google_services
    from googleapiclient.http import MediaFileUpload
    from datetime import datetime

    cfg = _tailoring_cfg(profile)
    parent_id = cfg.get("output_parent_folder_id")
    if not parent_id:
        raise ValueError("tailoring.output_parent_folder_id is not set in theo.yaml")

    company, datestr = "resume", datetime.now().strftime("%Y-%m-%d")
    score_path = Path(output_dir) / "score.json"
    if score_path.exists():
        try:
            import json
            score = json.loads(score_path.read_text())
            company = _slug(score.get("company", "resume")) or "resume"
        except Exception:
            pass

    drive, _ = get_google_services(profile)
    month = datetime.now().strftime("%Y_%m")
    folder_id = _find_or_create_folder(drive, month, parent_id)
    filename = f"TheoKelly_CV_{company}_{datestr}.pptx"
    media = MediaFileUpload(str(local_path),
                            mimetype="application/vnd.openxmlformats-officedocument.presentationml.presentation",
                            resumable=True)
    created = drive.files().create(
        body={"name": filename, "parents": [folder_id]},
        media_body=media,
        fields="id, webViewLink",
    ).execute()
    return {"file_id": created["id"],
            "web_link": created.get("webViewLink", ""),
            "folder_id": folder_id,
            "filename": filename}


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def resolve_pptx(version: str | None, profile: dict) -> Path:
    """Map a resume version to its source PPTX.

    Prefers the static Drive base (tailoring.base_pptx_drive_id) when set —
    download it with download_base_pptx first. Falls back to the local
    tailoring.pptx_map, then to the single PPTX in profile/resume_versions/.
    """
    pptx_map = _tailoring_cfg(profile).get("pptx_map") or {}
    if version and version in pptx_map:
        p = Path(pptx_map[version])
        if p.exists():
            return p
    candidates = sorted(Path("profile/resume_versions").glob("*.pptx"))
    if len(candidates) == 1:
        return candidates[0]
    raise FileNotFoundError(
        f"No PPTX mapped for resume version '{version}'. "
        "Add tailoring.pptx_map to theo.yaml."
    )


def tailor_from_output_dir(output_dir: Path | str, profile: dict,
                           dry_run: bool = False,
                           upload: bool | None = None) -> dict:
    """Run the full tailor flow for one application output directory.

    Always writes resume_tailored.pptx locally. When upload is enabled (or
    tailoring.upload_to_drive is true and output_parent_folder_id is set),
    also uploads to the current YYYY_MM folder in Drive.
    """
    import tempfile

    output_dir = Path(output_dir)
    rec_path = output_dir / "resume_recommendation.md"
    if not rec_path.exists():
        raise FileNotFoundError(f"No resume_recommendation.md in {output_dir}")
    version, edits = parse_recommendation(rec_path.read_text())

    cfg = _tailoring_cfg(profile)
    tmp_base = None
    if cfg.get("base_pptx_drive_id"):
        tmp_base = Path(tempfile.mkdtemp(prefix="tailor_base_")) / "base.pptx"
        pptx_path = download_base_pptx(profile, tmp_base)
        source_desc = f"Drive base ({cfg['base_pptx_drive_id']})"
    else:
        pptx_path = resolve_pptx(version, profile)
        source_desc = str(pptx_path)

    out_path = output_dir / "resume_tailored.pptx"
    report = apply_edits(pptx_path, edits, out_path, dry_run=dry_run)
    report["version"] = version
    report["source_pptx"] = source_desc
    report["edits_found"] = len(edits)

    if tmp_base and tmp_base.exists():
        tmp_base.unlink()

    want_upload = cfg.get("upload_to_drive", False) if upload is None else upload
    if want_upload and not dry_run:
        if cfg.get("output_parent_folder_id"):
            try:
                report["drive_upload"] = upload_tailored_pptx(profile, out_path, output_dir)
            except Exception as e:
                report["drive_upload_error"] = str(e)
        else:
            report["drive_upload_error"] = "tailoring.output_parent_folder_id is not set"
    return report


def format_report(report: dict) -> str:
    """Human-readable summary of a tailor run."""
    lines = [
        f"Tailored resume: {report['output_path']}{' (dry run — not written)' if report['dry_run'] else ''}",
        f"Source: {report['source_pptx']}  (version: {report.get('version')}, {report['edits_found']} edits found)",
        "",
    ]
    if report.get("drive_upload"):
        up = report["drive_upload"]
        lines.append(f"Drive: uploaded as {up['filename']}")
        if up.get("web_link"):
            lines.append(f"       {up['web_link']}")
        lines.append("")
    if report.get("drive_upload_error"):
        lines.append(f"Drive upload failed: {report['drive_upload_error']}")
        lines.append("")
    if report["applied"]:
        lines.append("Applied:")
        for r in report["applied"]:
            lines.append(f"  [{r['operation']}] {r['section']}: {r['detail']}")
    if report["flagged"]:
        lines.append("Needs your eye:")
        for r in report["flagged"]:
            sec = f"{r['section']}: " if r["section"] else ""
            lines.append(f"  ! {sec}{r['detail']}")
    if report["skipped"]:
        lines.append("Skipped:")
        for r in report["skipped"]:
            lines.append(f"  - [{r['operation']}] {r['section']}: {r['detail']}")
    if not report["applied"] and not report["flagged"] and not report["skipped"]:
        lines.append("No edits found in the recommendation.")
    return "\n".join(lines)
