"""job-agent doctor — health checks for the pipeline's environment and inputs.

Read-only: reports problems, changes nothing. Exit code 0 = healthy,
1 = errors found (warnings don't fail).

Checks:
  - profile/theo.yaml exists and passes validation
  - ANTHROPIC_API_KEY is set
  - generation profile files (cover-letter inputs) are present
  - resume .md files are in sync with their .pptx sources
  - Drive sync status (placeholder until Phase C1 lands)
"""
from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Issue:
    level: str  # "error" | "warning" | "info"
    area: str
    message: str
    hint: str = ""


@dataclass
class Report:
    issues: list[Issue] = field(default_factory=list)

    def add(self, level: str, area: str, message: str, hint: str = "") -> None:
        self.issues.append(Issue(level, area, message, hint))

    @property
    def has_errors(self) -> bool:
        return any(i.level == "error" for i in self.issues)


# Matches the source note written by scripts/extract_resume.py:
#   > Source: `TheoKelly_CV_Amsterdam.pptx` (in this folder; synced ...). ...
_SOURCE_RE = re.compile(r"^> Source: `([^`]+)`", re.M)


def _parse_source_pptx(md_text: str) -> str | None:
    """Return the .pptx filename cited in a resume .md's source note, if any."""
    m = _SOURCE_RE.search(md_text)
    return m.group(1) if m else None


def _md_title(md_text: str) -> str | None:
    """Return the markdown H1 (used as the extractor's --title)."""
    for line in md_text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return None


def check_profile(report: Report):
    """theo.yaml must exist and pass validation — every command loads it."""
    from .config import load_profile, ProfileError

    try:
        profile = load_profile("profile/theo.yaml")
    except ProfileError as e:
        report.add("error", "profile", f"theo.yaml invalid: {e}",
                   "Fix the yaml — every pipeline command loads it.")
        return None
    report.add("info", "profile", f"theo.yaml valid ({len(profile)} top-level keys)")
    return profile


def check_env(report: Report) -> None:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        report.add("error", "env", "ANTHROPIC_API_KEY not set",
                   "Add it to data/secrets.env or .env")
    else:
        report.add("info", "env", "ANTHROPIC_API_KEY present")
    if not Path("data").is_dir():
        report.add("warning", "env", "data/ directory missing",
                   "It is created on first run; nothing to do yet.")


def check_generation_files(report: Report) -> None:
    from .config import GENERATION_PROFILE_FILES

    missing = [f for f in GENERATION_PROFILE_FILES if not Path(f).exists()]
    for f in missing:
        report.add("warning", "profile-files", f"Missing {f}",
                   "Generation degrades gracefully, but output will lack this input.")
    if not missing:
        report.add("info", "profile-files",
                   f"All {len(GENERATION_PROFILE_FILES)} generation inputs present")


def _load_extractor():
    """Import scripts/extract_resume.py (not part of the installed package)."""
    scripts_dir = Path("scripts").resolve()
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    import extract_resume
    return extract_resume


def check_resume_sync(report: Report) -> None:
    """Every resume .md must match what the extractor would produce from its
    cited .pptx. Catches the 'edited the slides, forgot the markdown' drift."""
    rv = Path("profile/resume_versions")
    if not rv.is_dir():
        report.add("error", "resume-sync", "profile/resume_versions/ not found")
        return
    pptxs = {p.name: p for p in rv.glob("*.pptx")}
    mds = [p for p in sorted(rv.glob("*.md")) if p.stem != "README"]
    if not pptxs:
        report.add("info", "resume-sync", "No .pptx sources to check")
        return

    try:
        extractor = _load_extractor()
    except ImportError as e:
        report.add("warning", "resume-sync",
                   f"Extractor unavailable ({e}); skipping sync check",
                   "pip install python-pptx")
        return

    cited: set[str] = set()
    for md in mds:
        text = md.read_text()
        pptx_name = _parse_source_pptx(text)
        if not pptx_name:
            continue
        cited.add(pptx_name)
        pptx = pptxs.get(pptx_name)
        if pptx is None:
            report.add("warning", "resume-sync",
                       f"{md.name} cites {pptx_name}, which is not in resume_versions/")
            continue
        title = _md_title(text) or pptx.stem
        try:
            shapes = extractor.load_shapes(pptx)
            fresh = extractor.render(
                pptx, title,
                extractor.extract_main(shapes),
                extractor.render_sidebar(extractor.extract_sidebar(shapes)),
            )
        except Exception as e:
            report.add("warning", "resume-sync",
                       f"Could not extract {pptx_name}: {e}")
            continue
        if fresh == text:
            report.add("info", "resume-sync", f"{md.name} in sync with {pptx_name}")
        else:
            report.add(
                "error", "resume-sync",
                f"{md.name} is STALE vs {pptx_name}",
                f"Review: scripts/extract_resume.py {pptx} {md} --check "
                f"then re-run without --check to update",
            )
    for name in sorted(pptxs):
        if name not in cited:
            report.add("warning", "resume-sync", f"{name} has no markdown version",
                       "Extract one with scripts/extract_resume.py")


def check_application_answers(report: Report, profile: dict | None) -> None:
    """The application_answers block is optional, but an empty one means every
generated answers.md will be full of TODO markers. Nudge, don't fail."""
    answers = (profile or {}).get("application_answers") or {}
    filled = [k for k, v in answers.items() if v]
    if not filled:
        report.add("warning", "application-answers",
                   "application_answers block empty or missing in theo.yaml",
                   "Add salary_expectation, notice_period, etc. — see README")
    else:
        report.add("info", "application-answers",
                   f"{len(filled)} application answer(s) configured")


def check_drive(report: Report, profile: dict | None) -> None:
    """Drive sync status: configured? credentials present? files mapped?"""
    sync_cfg = (profile or {}).get("profile_sync")
    if not sync_cfg:
        report.add("info", "drive", "Drive sync not configured (add profile_sync to theo.yaml)")
        return
    creds_file = sync_cfg.get("credentials_file", "")
    if creds_file and Path(creds_file).exists():
        report.add("info", "drive", f"Drive credentials present ({creds_file})")
    else:
        report.add("warning", "drive",
                   f"Drive credentials not found: {creds_file or '(not set)'}",
                   "Run: job-agent drive-auth")
    files = sync_cfg.get("files") or []
    missing_local = [f["local_path"] for f in files
                     if isinstance(f, dict) and not Path(f.get("local_path", "")).exists()]
    for p in missing_local:
        report.add("warning", "drive", f"Mapped local file missing: {p}",
                   "Run: job-agent sync-profile --apply to fetch it from Drive")
    report.add("info", "drive", f"{len(files)} file(s) mapped for Drive sync")


_LEVEL_LABEL = {"error": "ERROR", "warning": "warn", "info": "ok"}


def run_doctor() -> int:
    """Run all checks, print the report, return 0 if healthy else 1."""
    report = Report()
    profile = check_profile(report)
    check_env(report)
    check_generation_files(report)
    check_resume_sync(report)
    check_drive(report, profile)
    check_application_answers(report, profile)

    print("\n=== job-agent doctor ===")
    for issue in report.issues:
        label = _LEVEL_LABEL[issue.level]
        print(f"[{label}] {issue.area}: {issue.message}")
        if issue.hint:
            print(f"       → {issue.hint}")
    errors = sum(1 for i in report.issues if i.level == "error")
    warnings = sum(1 for i in report.issues if i.level == "warning")
    print(f"\n{errors} error(s), {warnings} warning(s)\n")
    return 1 if report.has_errors else 0
