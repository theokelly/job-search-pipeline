"""Cover letter Google Doc builder.

Takes the generated cover_letter.md from an application output directory and
builds a styled Google Doc that mirrors Theo's sample cover letter, then
saves it to the current YYYY_MM folder in Drive — the same monthly-folder
flow as the tailored resume PPTX (see tailor.py).

Sample: TheoKelly_CoverLetter_Sample
    https://docs.google.com/document/d/1CH3njIqyCGDoQvZfbHZvvqql_dDlQxnTbQzbsPjSMHo/edit
    US Letter, 1" margins; Calibri throughout — name 13pt bold,
    contact/location 10pt, body 11pt at 115% line spacing with 10pt space
    below. 432 words fills exactly one page.

Sizing: new letters are word-count checked against the 432-word sample.
Over-length letters still build (trim in the Doc), but the report carries a
length_warning and the UI surfaces it prominently.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Sample reference
# ---------------------------------------------------------------------------

SAMPLE_DOC_ID = "1CH3njIqyCGDoQvZfbHZvvqql_dDlQxnTbQzbsPjSMHo"
SAMPLE_DOC_TITLE = "TheoKelly_CoverLetter_Sample"
# Full-page word count at the styles below. If the sample is ever restyled,
# recount its words and update this alongside _STYLES.
SAMPLE_WORD_COUNT = 432

_FONT = "Calibri"
# style_key -> (size_pt, bold, space_below_pt, line_spacing_pct or None)
_STYLES = {
    "name":           (13, True,   0, None),
    "contact":        (10, False,  0, None),
    "location":       (10, False,  0, None),
    "blank":          (10, False,  0, None),
    "date":           (11, False, 10, None),
    "recipient":      (11, False,  0, None),
    "recipient_last": (11, False, 10, None),
    "salutation":     (11, False, 10, 115),
    "body":           (11, False, 10, 115),
    "closing":        (11, False,  0, 115),
    "signature":      (11, False,  0, 115),
}

_CLOSING_PREFIXES = (
    "sincerely", "best regards", "kind regards", "warm regards",
    "respectfully",
)


def _word_count(text: str) -> int:
    return len(text.split())


def _u16_len(text: str) -> int:
    """Length in UTF-16 code units (what the Docs API uses for indices)."""
    return len(text.encode("utf-16-le")) // 2


# ---------------------------------------------------------------------------
# Markdown parsing
# ---------------------------------------------------------------------------

def parse_cover_letter_md(text: str) -> dict:
    """Parse the generated cover_letter.md into styled blocks.

    Expected layout (mirrors the sample): name, contact line, location,
    date, recipient block, salutation ("Dear ..."), body paragraphs,
    closing ("Sincerely,") + signature. Raises ValueError when the
    structure cannot be recognised.
    """
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    if len(lines) < 8:
        raise ValueError(
            f"cover_letter.md has only {len(lines)} non-empty lines; "
            "expected the full letter structure"
        )
    name, contact, location, date = lines[0], lines[1], lines[2], lines[3]

    sal_idx = next(
        (i for i in range(4, len(lines)) if lines[i].lower().startswith("dear")),
        None,
    )
    if sal_idx is None:
        raise ValueError(
            "Could not find the salutation (expected a 'Dear ...' line) "
            "in cover_letter.md"
        )
    recipient = lines[4:sal_idx]

    close_idx = next(
        (i for i in range(sal_idx + 1, len(lines))
         if lines[i].lower().startswith(_CLOSING_PREFIXES)),
        None,
    )
    if close_idx is None:
        raise ValueError(
            "Could not find the closing (expected 'Sincerely,') in cover_letter.md"
        )
    body = lines[sal_idx + 1:close_idx]
    if not body:
        raise ValueError(
            "No body paragraphs found between salutation and closing "
            "in cover_letter.md"
        )

    return {
        "name": name,
        "contact": contact,
        "location": location,
        "date": date,
        "recipient": recipient,
        "salutation": lines[sal_idx],
        "body": body,
        "closing": lines[close_idx:],
    }


def _styled_paragraphs(parsed: dict) -> list[tuple[str, str]]:
    """Flatten the parsed letter into (text, style_key) paragraphs."""
    paras = [
        (parsed["name"], "name"),
        (parsed["contact"], "contact"),
        (parsed["location"], "location"),
        ("", "blank"),
        (parsed["date"], "date"),
    ]
    recips = parsed["recipient"]
    for i, r in enumerate(recips):
        paras.append((r, "recipient_last" if i == len(recips) - 1 else "recipient"))
    paras.append((parsed["salutation"], "salutation"))
    paras.extend((b, "body") for b in parsed["body"])
    for i, c in enumerate(parsed["closing"]):
        paras.append((c, "signature" if i > 0 else "closing"))
    return paras


# ---------------------------------------------------------------------------
# Docs API request builders
# ---------------------------------------------------------------------------

def _document_style_request() -> dict:
    pt = lambda m: {"magnitude": m, "unit": "PT"}
    return {"updateDocumentStyle": {
        "documentStyle": {
            "pageSize": {"height": pt(792), "width": pt(612)},
            "marginTop": pt(72),
            "marginBottom": pt(72),
            "marginLeft": pt(72),
            "marginRight": pt(72),
        },
        "fields": "pageSize,marginTop,marginBottom,marginLeft,marginRight",
    }}


def _paragraph_requests(paras: list[tuple[str, str]]) -> tuple[str, list[dict]]:
    """Return (full_text, style requests) for an insertText at index 1."""
    full_text = "\n".join(t for t, _ in paras)
    requests: list[dict] = []
    idx = 1  # Docs body starts with an empty paragraph; index 1 is its start
    for text, style_key in paras:
        size, bold, space_below, line_spacing = _STYLES[style_key]
        ulen = _u16_len(text)
        if ulen:
            start, end = idx, idx + ulen
            pstyle: dict = {"alignment": "START"}
            fields = ["alignment"]
            pstyle["spaceBelow"] = {"magnitude": space_below, "unit": "PT"}
            fields.append("spaceBelow")
            if line_spacing is not None:
                pstyle["lineSpacing"] = line_spacing
                fields.append("lineSpacing")
            requests.append({"updateParagraphStyle": {
                "range": {"startIndex": start, "endIndex": end},
                "paragraphStyle": pstyle,
                "fields": ",".join(fields),
            }})
            requests.append({"updateTextStyle": {
                "range": {"startIndex": start, "endIndex": end},
                "textStyle": {
                    "weightedFontFamily": {"fontFamily": _FONT},
                    "fontSize": {"magnitude": size, "unit": "PT"},
                    "bold": bold,
                },
                "fields": "weightedFontFamily,fontSize,bold",
            }})
        idx += ulen + 1  # +1 for the paragraph-separating newline
    return full_text, requests


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def _cover_letter_filename(output_dir: Path) -> str:
    from .tailor import _slug
    company, datestr = "cover-letter", datetime.now().strftime("%Y-%m-%d")
    score_path = Path(output_dir) / "score.json"
    if score_path.exists():
        try:
            import json
            score = json.loads(score_path.read_text())
            company = _slug(score.get("company", "cover-letter")) or "cover-letter"
        except Exception:
            pass
    return f"TheoKelly_CoverLetter_{company}_{datestr}"


def build_cover_letter_doc(output_dir: Path | str, profile: dict) -> dict:
    """Build a styled Google Doc from cover_letter.md; upload to YYYY_MM Drive folder.

    Returns {file_id, web_link, filename, folder_id, word_count,
             sample_word_count, length_warning}.
    """
    from .drive_sync import get_google_services
    from .tailor import _find_or_create_folder

    output_dir = Path(output_dir)
    md_path = output_dir / "cover_letter.md"
    if not md_path.exists():
        raise FileNotFoundError(f"No cover_letter.md in {output_dir}")
    md_text = md_path.read_text()
    parsed = parse_cover_letter_md(md_text)

    word_count = _word_count(md_text)
    length_warning = None
    if word_count > SAMPLE_WORD_COUNT:
        length_warning = (
            f"Letter is {word_count} words vs the {SAMPLE_WORD_COUNT}-word "
            f"one-page sample ({word_count - SAMPLE_WORD_COUNT} over) — "
            "likely spills past one page; trim before sending."
        )

    cfg = profile.get("tailoring") or {}
    parent_id = cfg.get("output_parent_folder_id")
    if not parent_id:
        raise ValueError("tailoring.output_parent_folder_id is not set in theo.yaml")

    filename = _cover_letter_filename(output_dir)
    paras = _styled_paragraphs(parsed)
    full_text, style_requests = _paragraph_requests(paras)

    drive, docs = get_google_services(profile)
    created = docs.documents().create(body={"title": filename}).execute()
    doc_id = created["documentId"]

    requests = [
        _document_style_request(),
        {"insertText": {"location": {"index": 1}, "text": full_text}},
        *style_requests,
    ]
    docs.documents().batchUpdate(
        documentId=doc_id, body={"requests": requests}
    ).execute()

    month = datetime.now().strftime("%Y_%m")
    folder_id = _find_or_create_folder(drive, month, parent_id)
    moved = drive.files().update(
        fileId=doc_id,
        addParents=folder_id,
        removeParents="root",
        fields="id,webViewLink",
    ).execute()

    return {
        "file_id": doc_id,
        "web_link": moved.get("webViewLink", ""),
        "filename": filename,
        "folder_id": folder_id,
        "word_count": word_count,
        "sample_word_count": SAMPLE_WORD_COUNT,
        "length_warning": length_warning,
    }
