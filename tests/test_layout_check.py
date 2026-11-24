"""Tests for the render-aware overflow check (layout_check.py).

The Omnicom regression (Nov 24, 2026): two bullets grew by 18 chars total
— inside every char budget — but re-wrapped from 13 to 14 lines in a frame
fitted for 13, printing the last bullet over the Tech line. The check
compares estimated wrapped-line counts before/after an edit, using the
same glyph metrics for both so systematic error cancels.

Tests inject a fixed-width measurer: production measures with Nimbus Sans
via PIL and fails open when the font is absent (CI), so nothing here
depends on a real font file.
"""
from pptx import Presentation
from pptx.util import Inches, Pt

from job_agent import layout_check


def _measure(text, size_pt):
    return len(text) * size_pt * 0.01  # 0.12in per char at 12pt


def _deck(top_text, below_top=2.2):
    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    a = slide.shapes.add_textbox(Inches(1), Inches(1), Inches(4), Inches(1))
    run = a.text_frame.paragraphs[0].add_run()
    run.text = top_text
    run.font.size = Pt(12)
    b = slide.shapes.add_textbox(Inches(1), Inches(below_top), Inches(4), Inches(1))
    run = b.text_frame.paragraphs[0].add_run()
    run.text = "Tech: Python"
    run.font.size = Pt(12)
    return prs, a


def test_wrap_lines_counts():
    # 4in line, 0.12in/char -> ~33 chars per line
    assert layout_check._wrap_lines("x" * 30, 12, 4.0, 4.0, measure=_measure) == 1
    assert layout_check._wrap_lines(" ".join(["word"] * 20), 12, 4.0, 4.0,
                                    measure=_measure) == 4


def test_growth_past_next_element_flags():
    prs, a = _deck(" ".join(["word"] * 12))          # ~2 lines, fits
    snap = layout_check.snapshot_lines(prs, measure=_measure)
    a.text_frame.paragraphs[0].runs[0].text = " ".join(["word"] * 80)  # ~13 lines
    flags = layout_check.flags_from_snapshot(prs, snap, measure=_measure)
    assert len(flags) == 1
    assert flags[0]["operation"] == "layout-check"


def test_same_lines_no_flag():
    prs, a = _deck(" ".join(["word"] * 12))
    snap = layout_check.snapshot_lines(prs, measure=_measure)
    a.text_frame.paragraphs[0].runs[0].text = " ".join(["term"] * 12)
    assert layout_check.flags_from_snapshot(prs, snap, measure=_measure) == []


def test_unmeasurable_fails_open():
    prs, _ = _deck("anything")
    assert layout_check.snapshot_lines(prs, measure=lambda t, s: None) == {}
