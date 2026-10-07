#!/usr/bin/env python3
"""Extract resume text from a PPTX into the markdown format used by
profile/resume_versions/*.md.

Purpose-built for Theo's Dutch-style two-page CV (Google Slides export):
a navy sidebar column plus a main column, with consistent text styles
driving the markdown structure. Not a general PPTX->markdown converter.

Usage:
    scripts/extract_resume.py <input.pptx> <output.md> [--title "..."] [--check]

--check exits 0 if the output would be byte-identical, 1 otherwise
(prints a unified diff). Never overwrites without showing the diff first:
run with --check, review, then run without it.
"""

from __future__ import annotations

import argparse
import difflib
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from pptx import Presentation

# ---------------------------------------------------------------------------
# Geometry thresholds (EMU). Sidebar = navy left column; main = content column.
# ---------------------------------------------------------------------------
SIDEBAR_MAX_X = 2_000_000
DATES_MIN_X = 6_000_000

NAVY = "1C3A6B"
GRAY = "6B7280"
DARK = "1A1A2E"
BODY_GRAY = "3D3D5C"
SIDEBAR_LABEL = "C9DAF8"
WHITE = "FFFFFF"

CANONICAL_ORDER = [
    "Header",
    "Personal Info (Sidebar)",
    "Languages",
    "Introduction",
    "Work Experience",
    "Early Career",
    "Education",
    "Tech Stack (Sidebar)",
    "Working Style (Sidebar)",
    "Activities and Interests",
    "Board / Volunteer",
]

SOURCE_NOTE = (
    "> Source: `{pptx_name}` (in this folder; synced {synced}). "
    "This is the text content of the primary Dutch-style two-page A4 CV. "
    "The PPTX remains the source of truth for rendering. "
    "Update this file when content changes meaningfully (not styling)."
)


@dataclass
class Para:
    text: str
    size: float | None
    bold: bool | None
    color: str | None


@dataclass
class Shape:
    name: str
    x: int
    y: int
    paras: list[Para] = field(default_factory=list)
    slide: int = 0

    @property
    def text(self) -> str:
        return "\n".join(p.text for p in self.paras)

    @property
    def first(self) -> Para:
        return self.paras[0]


def _color_str(font) -> str | None:
    try:
        rgb = font.color.rgb
        return str(rgb) if rgb else None
    except Exception:
        return None


def load_shapes(pptx_path: Path) -> list[Shape]:
    prs = Presentation(str(pptx_path))
    shapes: list[Shape] = []
    seen: dict[str, int] = {}
    for slide_idx, slide in enumerate(prs.slides):
        for s in slide.shapes:
            if not s.has_text_frame:
                continue
            paras: list[Para] = []
            for p in s.text_frame.paragraphs:
                runs = []
                for r in p.runs:
                    if not r.text:
                        continue
                    f = r.font
                    runs.append((r.text, f.size.pt if f.size else None, f.bold, _color_str(f)))
                if not runs:
                    paras.append(Para("", None, None, None))
                    continue
                # Merge runs; keep style of the first non-empty run for classification.
                text = "".join(t for t, _, _, _ in runs)
                _, size, bold, color = runs[0]
                paras.append(Para(text, size, bold, color))
            full = "\n".join(p.text for p in paras).strip()
            if not full:
                continue
            # Dedupe Google-Slides duplicate-slide cruft (slide 2 carries leftover
            # copies of slide 1's sidebar): skip sidebar text already seen on an
            # *earlier* slide. Main-column and dates repeats are legitimate
            # (e.g. an institution under two degrees, or '2023' twice).
            if full in seen and seen[full] < slide_idx and s.left.emu < SIDEBAR_MAX_X:
                continue
            seen[full] = slide_idx
            sh = Shape(s.name, s.left.emu, s.top.emu, paras)
            sh.slide = slide_idx
            shapes.append(sh)
    return shapes


def _clean(text: str) -> str:
    text = (
        text.replace("\u2019", "'")
        .replace("\u2018", "'")
        .replace("\u201c", '"')
        .replace("\u201d", '"')
    )
    return re.sub(r"\s+", " ", text).strip()


def _split_company(text: str) -> tuple[str, str]:
    """'FB Solutions Group | Chicago, IL, USA' -> (company, location)."""
    parts = [p.strip() for p in _clean(text).split("|")]
    if len(parts) >= 2:
        return parts[0], " | ".join(parts[1:])
    return _clean(text), ""


# ---------------------------------------------------------------------------
# Main-column extraction
# ---------------------------------------------------------------------------

@dataclass
class WorkBlock:
    company: str
    location: str
    dates: str
    role: str = ""
    desc: str = ""
    bullets: list[str] = field(default_factory=list)
    tech: str = ""


@dataclass
class EduBlock:
    institution: str
    dates: str
    degree: str = ""
    desc: str = ""
    courses: str = ""
    extra_degrees: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class BoardBlock:
    org: str
    dates: str
    role: str = ""
    desc: str = ""


def extract_main(shapes: list[Shape]) -> dict:
    dates = [s for s in shapes if s.x >= DATES_MIN_X]

    def find_dates(y: int) -> str:
        best, best_dy = "", 200_000
        for d in dates:
            dy = abs(d.y - y)
            if dy < best_dy:
                best, best_dy = d.text.strip(), dy
        return best

    sections: dict[str, list] = {}
    header_name = ""
    header_title = ""
    current: str | None = None
    pending: WorkBlock | EduBlock | BoardBlock | None = None

    def flush():
        nonlocal pending
        if pending is not None and current is not None:
            sections.setdefault(current, []).append(pending)
        pending = None

    def paras_of(s: Shape) -> list[str]:
        return [_clean(pp.text) for pp in s.paras if pp.text.strip()]

    n_slides = max(s.slide for s in shapes) + 1
    for slide_idx in range(n_slides):
        main = sorted(
            [s for s in shapes if s.slide == slide_idx and SIDEBAR_MAX_X <= s.x < DATES_MIN_X],
            key=lambda s: s.y,
        )
        for s in main:
            p0 = s.first
            txt = _clean(s.text)
            if not txt:
                continue
            size, bold, color = p0.size, p0.bold, p0.color

            if current is None and size and size >= 20 and bold:
                header_name = txt
                continue
            if current is None and size == 10 and not bold:
                header_title = txt
                continue
            if size == 13 and bold and color == NAVY:
                flush()
                current = txt
                continue
            if current is None:
                continue

            if current in ("Work Experience", "Early Career"):
                if size == 10 and bold:
                    flush()
                    company, location = _split_company(s.text)
                    pending = WorkBlock(company, location, find_dates(s.y))
                elif isinstance(pending, WorkBlock):
                    if size == 9 and bold:
                        pending.role = txt
                    elif size == 8 and color == GRAY:
                        pending.desc = txt
                    elif size == 8.5:
                        pending.bullets = paras_of(s)
                    elif txt.startswith("Tech:"):
                        pending.tech = txt[len("Tech:"):].strip()
            elif current == "Education":
                if size == 10 and bold:
                    flush()
                    pending = EduBlock("", find_dates(s.y), degree=txt)
                elif isinstance(pending, EduBlock):
                    if size == 9 and not bold:
                        pending.institution = txt
                    elif size == 8.5:
                        for para in paras_of(s):
                            if para.startswith("Courses:"):
                                pending.courses = para
                            else:
                                pending.desc = para
            elif current in ("Introduction", "Activities and Interests"):
                if size == 10 and bold:
                    # Board items follow Activities with no header of their own
                    flush()
                    current = "Board / Volunteer"
                    pending = BoardBlock(_clean(s.text), find_dates(s.y))
                else:
                    sections.setdefault(current, []).extend(paras_of(s))
            elif current == "Board / Volunteer":
                if size == 10 and bold:
                    flush()
                    pending = BoardBlock(_clean(s.text), find_dates(s.y))
                elif isinstance(pending, BoardBlock):
                    if size == 9 and not bold:
                        pending.role = txt
                    elif size == 8:
                        pending.desc = txt
    flush()

    # Merge consecutive education blocks sharing institution + dates
    for sec, blocks in sections.items():
        merged: list = []
        for b in blocks:
            if isinstance(b, EduBlock):
                b.extra_degrees = []
            if (isinstance(b, EduBlock) and merged and isinstance(merged[-1], EduBlock)
                    and merged[-1].institution == b.institution
                    and merged[-1].dates == b.dates and b.institution):
                merged[-1].extra_degrees.append((b.degree, b.courses))
            else:
                merged.append(b)
        sections[sec] = merged
    return {"_header": (header_name, header_title), **sections}


# ---------------------------------------------------------------------------
# Sidebar extraction
# ---------------------------------------------------------------------------

def extract_sidebar(shapes: list[Shape]) -> dict:
    side = [s for s in shapes if s.x < SIDEBAR_MAX_X]
    headers = sorted(
        [s for s in side if s.first.size == 9.5 and s.first.bold and s.first.color == WHITE],
        key=lambda s: (s.slide, s.y),
    )
    sections: dict[str, list[Shape]] = {}
    for s in side:
        if s in headers:
            sections.setdefault(s.first.text.strip(), [])
            continue
        # Assign to the nearest header at or above this shape (same slide)
        best = None
        for h in headers:
            if h.slide == s.slide and h.y <= s.y:
                best = h
        if best is not None:
            sections.setdefault(best.first.text.strip(), []).append(s)
    return sections


def render_sidebar(sections: dict[str, list[Shape]]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for name, shapes in sections.items():
        if name == "Personal Info":
            lines = []
            by_y = sorted(shapes, key=lambda s: (s.y, s.x))
            i = 0
            while i < len(by_y):
                s = by_y[i]
                p = s.first
                if p.size == 7.5 and p.bold and p.color == SIDEBAR_LABEL:
                    label = p.text.strip()
                    vals = []
                    j = i + 1
                    while j < len(by_y):
                        q = by_y[j].first
                        if q.size == 7.5 and q.bold and q.color == SIDEBAR_LABEL:
                            break
                        if q.size == 8.0 and q.color == WHITE:
                            vals.append(by_y[j].text.strip())
                        j += 1
                    lines.append(f"- **{label}:** {' '.join(vals)}")
                    i = j
                else:
                    i += 1
            out["Personal Info (Sidebar)"] = lines
        elif name == "Languages":
            lines = []
            # name (8pt white) + level (7.5pt light blue) share a row
            rows: dict[int, list[Shape]] = {}
            for s in shapes:
                rows.setdefault(round(s.y / 100_000), []).append(s)
            for _, row in sorted(rows.items()):
                row = sorted(row, key=lambda s: s.x)
                parts = [r.text.strip() for r in row if r.text.strip()]
                if len(parts) >= 2:
                    lines.append(f"- {parts[0]} \u2014 {parts[1]}")
                elif parts:
                    lines.append(f"- {parts[0]}")
            out["Languages"] = lines
        elif name == "Tech Stack":
            lines = []
            by_y = sorted(shapes, key=lambda s: (s.y, s.x))
            i = 0
            while i < len(by_y):
                s = by_y[i]
                p = s.first
                if p.size == 7.5 and p.bold and p.color == SIDEBAR_LABEL:
                    label = p.text.strip()
                    vals = []
                    j = i + 1
                    while j < len(by_y):
                        q = by_y[j].first
                        if q.size == 7.5 and q.bold and q.color == SIDEBAR_LABEL:
                            break
                        if q.color == WHITE:
                            vals.append(" ".join(
                                pp.text.strip() for pp in by_y[j].paras if pp.text.strip()
                            ))
                        j += 1
                    lines.append(f"- **{label}:** {', '.join(v for v in vals if v)}")
                    i = j
                else:
                    i += 1
            out["Tech Stack (Sidebar)"] = lines
        elif name == "Working Style":
            words = [_clean(s.text) for s in shapes if s.text.strip()]
            out["Working Style (Sidebar)"] = [", ".join(words)]
    return out


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def render(pptx_path: Path, title: str, main: dict, sidebar: dict[str, list[str]]) -> str:
    synced = datetime.fromtimestamp(pptx_path.stat().st_mtime).strftime("%Y-%m-%d")
    L: list[str] = [f"# {title}", ""]
    L.append(SOURCE_NOTE.format(pptx_name=pptx_path.name, synced=synced))
    L.append("")

    name, role_title = main.get("_header", ("", ""))
    L.append("## Header")
    L.append("")
    L.append(f"**{name}**")
    L.append(f"**{role_title}**")
    L.append("")

    for sec in CANONICAL_ORDER[1:]:
        if sec in sidebar:
            L.append(f"## {sec}")
            L.append("")
            L.extend(sidebar[sec])
            L.append("")
        elif sec in main:
            L.append(f"## {sec}")
            L.append("")
            for block in main[sec]:
                if isinstance(block, str):
                    paras = [p for p in block.split("\n") if p.strip()]
                    L.extend(paras)
                    L.append("")
                elif isinstance(block, WorkBlock):
                    L.append(f"### {block.company} | {block.location} | {block.dates}")
                    if block.role:
                        L.append(f"**{block.role}**")
                        L.append("")
                    if block.desc:
                        L.append(block.desc)
                        L.append("")
                    for b in block.bullets:
                        L.append(f"- {b}")
                    if block.bullets:
                        L.append("")
                    if block.tech:
                        L.append(f"**Tech:** {block.tech}")
                        L.append("")
                elif isinstance(block, EduBlock):
                    L.append(f"### {block.institution} | {block.dates}")
                    degrees = [(block.degree, block.courses)] + block.extra_degrees
                    for degree, courses in degrees:
                        if degree and courses and not block.desc:
                            L.append(f"**{degree}** \u2014 {courses}")
                        else:
                            if degree:
                                L.append(f"**{degree}**")
                                L.append("")
                    if block.desc:
                        L.append(block.desc)
                        L.append("")
                        if block.courses:
                            L.append(block.courses)
                            L.append("")
                    elif not any(c for _, c in degrees):
                        L.append("")
                elif isinstance(block, BoardBlock):
                    L.append(f"- **{block.org}** | {block.role} | {block.dates}")
                    if block.desc:
                        L.append(f"  {block.desc}")
            if L and L[-1] == "":
                pass
            L.append("")
    text = "\n".join(L)
    # Collapse 3+ newlines, ensure single trailing newline
    text = re.sub(r"\n{3,}", "\n\n", text).rstrip("\n") + "\n"
    return text


def main() -> int:
    ap = argparse.ArgumentParser(description="Extract resume PPTX to markdown.")
    ap.add_argument("pptx", type=Path)
    ap.add_argument("output", type=Path)
    ap.add_argument("--title", default="Resume: AI-Focused (Primary Version)")
    ap.add_argument("--check", action="store_true",
                    help="diff against output instead of writing")
    args = ap.parse_args()

    shapes = load_shapes(args.pptx)
    main_data = extract_main(shapes)
    sidebar_data = render_sidebar(extract_sidebar(shapes))
    text = render(args.pptx, args.title, main_data, sidebar_data)

    if args.check:
        old = args.output.read_text() if args.output.exists() else ""
        if old == text:
            print("identical")
            return 0
        for line in difflib.unified_diff(
            old.splitlines(), text.splitlines(),
            str(args.output), "extracted", lineterm="",
        ):
            print(line)
        return 1
    args.output.write_text(text)
    print(f"wrote {args.output} ({len(text)} chars)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
