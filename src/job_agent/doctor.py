"""job-agent doctor — health checks for the pipeline's environment and inputs.

Read-only: reports problems, changes nothing. Exit code 0 = healthy,
1 = errors found (warnings don't fail).

Checks:
  - profile/theo.yaml exists and passes validation
  - ANTHROPIC_API_KEY is set
  - generation profile files (cover-letter inputs) are present
  - the resume mirror is in sync with the Drive base resume
  - Drive sync status (placeholder until Phase C1 lands)
"""
from __future__ import annotations

import os
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


def check_resume_sync(report: Report, profile: dict | None) -> None:
    """The resume mirror must match a fresh extraction from the Drive base.
    Catches the 'edited the Slides doc, mirror is stale' drift. The mirror
    auto-refreshes on generate, so a stale report here means the base
    changed since the last generation run."""
    from . import resume_mirror

    mirror = resume_mirror.MIRROR_PATH
    if not mirror.exists():
        report.add("error", "resume-sync", "profile/resume_base.md not found",
                   "It is generated from the Drive base — run any generate command.")
        return
    base_id = ((profile or {}).get("tailoring") or {}).get("base_pptx_drive_id")
    if not base_id:
        report.add("warning", "resume-sync",
                   "tailoring.base_pptx_drive_id not set; cannot verify the mirror")
        return
    try:
        fresh = resume_mirror.render_current_mirror(profile)
    except Exception as e:
        report.add("warning", "resume-sync",
                   f"Could not render the mirror from the Drive base ({e}); "
                   "skipping sync check")
        return
    if fresh == mirror.read_text():
        report.add("info", "resume-sync", "resume_base.md in sync with the Drive base")
    else:
        report.add("error", "resume-sync",
                   "resume_base.md is STALE vs the Drive base",
                   "Run any generate command — the mirror refreshes "
                   "automatically when the Drive doc changes.")


def check_fanout(report: Report, profile: dict | None) -> None:
    """Fan-out is optional, but if the profile configures it, its Brave key
    should be present — otherwise every --fanout run notes a skip error."""
    fanout = (profile or {}).get("fanout")
    if not fanout:
        report.add("info", "fanout", "Fan-out discovery not configured (optional; add a fanout block to theo.yaml)")
        return
    provider = (fanout.get("provider") or "brave").lower()
    if provider != "brave":
        report.add("warning", "fanout", f"Unsupported fanout provider '{provider}'", "Only 'brave' is implemented")
        return
    if os.environ.get("BRAVE_API_KEY") or os.environ.get("BRAVE_SEARCH_API_KEY"):
        report.add("info", "fanout", "Fan-out configured (Brave), API key present")
    else:
        report.add("warning", "fanout",
                   "Fan-out configured (Brave) but no API key in the environment",
                   "Add BRAVE_API_KEY to .env in the repo root — create the key at https://api-dashboard.search.brave.com")


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
    check_resume_sync(report, profile)
    check_drive(report, profile)
    check_fanout(report, profile)
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
