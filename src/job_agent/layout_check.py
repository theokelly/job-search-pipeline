"""Render-aware overflow check for tailored resume decks.

The tailor's char budgets cannot see line wraps: a replacement with the
same character count can wrap to more lines than the original (long words
land differently), and this deck's frames are fitted to their base text —
one extra line spills onto the element below (Nov 24, 2026: an Omnicom
bullet grew the FB Solutions block by a line and printed it over the
Tech line; every char-based flag stayed silent).

This module estimates wrapped line counts with real glyph metrics
(Nimbus Sans, metric-compatible with the deck's Helvetica Neue) and
flags any edited shape whose estimated text height newly exceeds the
space before the next element. Estimation errors are systematic, so
comparing before/after with the same ruler cancels most of them; the
check only fires when the edits ADD lines past the available space.

Fail-open: without PIL or the font (e.g. CI), measurement is unavailable
and the check reports nothing — the char-based flags still apply.
"""

from __future__ import annotations

from pptx.util import Emu

# Rendered line pitch = font size x this natural factor x the paragraph's
# lnSpc factor. 1.15 is calibrated against Google Slides' rendering of the
# base deck (Helvetica family): the base FB block estimates to 1.86in
# against 1.93in available, and one added line (+0.14in) tips it over —
# matching the observed fit/overflow boundary exactly.
_NATURAL_FACTOR = 1.15
_FONT_PATHS = [
    "/usr/share/fonts/opentype/urw-base35/NimbusSans-Regular.otf",
    "/usr/share/fonts/truetype/urw-base35/NimbusSans-Regular.ttf",
    "/usr/share/fonts/opentype/urw-base35/NimbusSans-Regular.ttf",
]
_font_cache: dict = {}
_font_unavailable = False


def _measure_text(text: str, size_pt: float) -> float | None:
    """Width of `text` in inches at `size_pt`, or None when unmeasurable."""
    global _font_unavailable
    if _font_unavailable:
        return None
    try:
        from PIL import ImageFont
    except ImportError:
        _font_unavailable = True
        return None
    key = round(size_pt * 4)
    if key not in _font_cache:
        font = None
        for path in _FONT_PATHS:
            try:
                font = ImageFont.truetype(path, size=key)
                break
            except (OSError, IOError):
                continue
        if font is None:
            _font_unavailable = True
            return None
        _font_cache[key] = font
    return _font_cache[key].getlength(text) / 4.0 / 72.0


def _wrap_lines(text: str, size_pt: float, first_w: float, rest_w: float,
                measure=None) -> int:
    measure = measure or _measure_text
    words = (text or "").split()
    if not words:
        return 1
    space = measure(" ", size_pt)
    if space is None:
        return -1  # unmeasurable
    lines = 1
    avail = first_w
    cur = 0.0
    for word in words:
        ww = measure(word, size_pt)
        if cur > 0 and cur + space + ww > avail:
            lines += 1
            avail = rest_w
            cur = ww
        else:
            cur = ww if cur == 0 else cur + space + ww
    return lines


def _paragraph_metrics(para):
    """(font_size_pt, line_factor, space_before_in, space_after_in)."""
    size = None
    for run in para.runs:
        if run.font.size:
            size = run.font.size.pt
            break
    factor = 1.0  # the paragraph's lnSpc factor; natural leading applied later
    before = after = 0.0
    pPr = para._p.find("{http://schemas.openxmlformats.org/drawingml/2006/main}pPr")
    if pPr is not None:
        ns = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
        ln = pPr.find(f"{ns}lnSpc/{ns}spcPct")
        if ln is not None:
            factor = int(ln.get("val")) / 100000.0
        for tag, attr in (("spcBef", "before"), ("spcAft", "after")):
            el = pPr.find(f"{ns}{tag}/{ns}spcPts")
            if el is not None:
                if attr == "before":
                    before = int(el.get("val")) / 100.0 / 72.0
                else:
                    after = int(el.get("val")) / 100.0 / 72.0
    return size, factor, before, after


def _shape_estimate(shape, measure=None):
    """(total_lines, est_height_in) for a text shape, or None."""
    tf = shape.text_frame
    width_in = (Emu(shape.width).inches - Emu(tf.margin_left).inches
                - Emu(tf.margin_right).inches)
    total_lines = 0
    height = Emu(tf.margin_top).inches + Emu(tf.margin_bottom).inches
    default_size = None
    for para in tf.paragraphs:
        if not para.text.strip():
            continue
        size, factor, before, after = _paragraph_metrics(para)
        if size is None:
            size = default_size or 12.0
        default_size = size
        pPr = para._p.find("{http://schemas.openxmlformats.org/drawingml/2006/main}pPr")
        marL = indent = 0.0
        if pPr is not None:
            if pPr.get("marL"):
                marL = int(pPr.get("marL")) / 914400
            if pPr.get("indent"):
                indent = int(pPr.get("indent")) / 914400
        lines = _wrap_lines(para.text, size, width_in - (marL + indent),
                            width_in - marL, measure=measure)
        if lines < 0:
            return None
        total_lines += lines
        height += lines * size * _NATURAL_FACTOR * factor / 72.0 + before + after
    return total_lines, height


def _available_height(slide, shape, slide_height_in: float) -> float:
    top = Emu(shape.top).inches
    left = Emu(shape.left).inches
    right = left + Emu(shape.width).inches
    best = slide_height_in
    for other in slide.shapes:
        if other is shape or not getattr(other, "has_text_frame", False):
            continue
        o_top = Emu(other.top).inches
        if o_top <= top + 0.05:
            continue
        o_left = Emu(other.left).inches
        o_right = o_left + Emu(other.width).inches
        overlap = min(right, o_right) - max(left, o_left)
        if abs(o_left - left) < 0.3 or overlap > 0.25 * (right - left):
            best = min(best, o_top)
    return best - top


def snapshot_lines(prs, measure=None) -> dict:
    """Per-shape layout estimates for every text shape: {(slide, id): info}."""
    snap = {}
    slide_h = Emu(prs.slide_height).inches
    for idx, slide in enumerate(prs.slides):
        for shape in slide.shapes:
            if not getattr(shape, "has_text_frame", False):
                continue
            est = _shape_estimate(shape, measure=measure)
            if est is None:
                continue
            lines, height = est
            snap[(idx, shape.shape_id)] = {
                "lines": lines,
                "height": height,
                "avail": _available_height(slide, shape, slide_h),
                "snippet": shape.text_frame.text.strip().split("\n")[0][:60],
            }
    return snap


def flags_from_snapshot(prs, snap: dict, measure=None) -> list[dict]:
    """Flag shapes whose edits added lines past the available space."""
    flags = []
    current = snapshot_lines(prs, measure=measure)
    for key, now in current.items():
        before = snap.get(key)
        if before is None:
            continue
        if now["lines"] > before["lines"] and now["height"] > now["avail"]:
            flags.append({
                "section": "",
                "operation": "layout-check",
                "status": "flagged",
                "detail": (
                    f"layout: '{now['snippet']}…' now needs ~{now['lines']} lines "
                    f"({now['height']:.2f}in) vs {now['avail']:.2f}in before the "
                    f"next element (was {before['lines']} lines) — likely "
                    "overlaps; eyeball the rendered deck"),
            })
    return flags
