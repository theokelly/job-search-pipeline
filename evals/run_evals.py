"""Golden-set regression evals for the prescreen and scoring stages.

Why this exists: the prescreen once drifted for weeks, issuing verdicts
on a 1,000-character excerpt that was mostly company boilerplate, and
nothing in the system noticed — the failure surfaced only when a second,
independent process disagreed. These evals turn that class of failure
into a number that moves: a small frozen set of real postings with known
expected outcomes, re-run on demand after any change to the prescreen
config, prompts, truncation caps, or model versions.

Assertion model:
  - Prescreen: exact verdict match. It is cheap and near-deterministic,
    so a flip is a real signal. Any prescreen miss fails the run.
  - Scoring: tier match plus subscore bands (+/- BAND) around the
    reference scores recorded when the case was admitted. The scorer is
    not deterministic; exact numbers would flap. Band misses are
    reported as warnings, not failures.

Cases live in evals/golden_cases.json (gitignored — real JD text and
company names; rebuild with scripts/build_golden_cases.py). Results are
written to evals/results/<timestamp>.md keyed by opaque case slug and
are safe to commit.

Usage:
    .venv/bin/python evals/run_evals.py [--stage prescreen|score|all]
"""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from job_agent.config import load_profile, load_text  # noqa: E402
from job_agent.db import Job  # noqa: E402
from job_agent.scoring.prescreen import prescreen_job  # noqa: E402
from job_agent.scoring.score import score_job  # noqa: E402
from job_agent.stages._common import NARRATIVE_PATH, PROFILE_PATH_DEFAULT  # noqa: E402

BAND = 15
CASES_PATH = Path("evals/golden_cases.json")
RESULTS_DIR = Path("evals/results")


def verdict_pass(expected: str, approved: bool) -> bool:
    return (expected == "approved") == approved


def score_pass(expected_tier, ref_skill, ref_pref, score, band: int = BAND) -> bool:
    """Tier must match exactly; subscores must sit within +/- band of the
    reference values when references were recorded for the case."""
    if score.tier != expected_tier:
        return False
    if ref_skill is not None and abs(score.skill_fit - ref_skill) > band:
        return False
    if ref_pref is not None and abs(score.preference_alignment - ref_pref) > band:
        return False
    return True


def _case_job(case: dict) -> Job:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    return Job(
        id=case["slug"], source="golden", company=case["company"],
        title=case["title"], location=case["location"], url=case["url"],
        description_raw=case["jd_text"], first_seen=now, last_seen=now,
        status="active",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", choices=["prescreen", "score", "all"], default="all")
    parser.add_argument("--cases", default=str(CASES_PATH))
    args = parser.parse_args()

    cases_file = Path(args.cases)
    if not cases_file.exists():
        print(f"No cases file at {cases_file}. Build it with scripts/build_golden_cases.py.")
        return 2
    cases = json.loads(cases_file.read_text())["cases"]

    profile = load_profile(PROFILE_PATH_DEFAULT)
    narrative = load_text(NARRATIVE_PATH)

    rows = []
    prescreen_hits = prescreen_total = 0
    score_hits = score_total = 0
    for case in cases:
        job = _case_job(case)
        approved, reason = prescreen_job(job, profile)
        prescreen_total += 1
        ok = verdict_pass(case["expected_prescreen"], approved)
        prescreen_hits += ok
        row = {
            "slug": case["slug"],
            "expected_prescreen": case["expected_prescreen"],
            "actual_prescreen": "approved" if approved else "rejected",
            "prescreen_pass": ok,
            "reason": reason,
            "expected_tier": case["expected_tier"],
            "actual_tier": None, "skill_fit": None, "preference_alignment": None,
            "score_pass": None,
        }
        if args.stage in ("score", "all") and case["expected_tier"] and approved:
            score = score_job(job, 0, profile, narrative)
            score_total += 1
            ok_s = score_pass(case["expected_tier"], case["ref_skill"],
                              case["ref_pref"], score)
            score_hits += ok_s
            row.update(actual_tier=score.tier, skill_fit=score.skill_fit,
                       preference_alignment=score.preference_alignment,
                       score_pass=ok_s)
        rows.append(row)
        mark = "PASS" if row["prescreen_pass"] else "FAIL"
        extra = ""
        if row["actual_tier"]:
            extra = (f" | tier {row['actual_tier']} (exp {row['expected_tier']}) "
                     f"skill {row['skill_fit']} pref {row['preference_alignment']} "
                     f"| score {'PASS' if row['score_pass'] else 'WARN'}")
        print(f"[{mark}] {row['slug']} [{case['company']} — {case['title']}]: "
              f"prescreen {row['actual_prescreen']} "
              f"(exp {row['expected_prescreen']}){extra}")

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H%M")
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out = RESULTS_DIR / f"{stamp}.md"
    lines = [
        f"# Eval run {stamp} UTC",
        "",
        f"Prescreen agreement: {prescreen_hits}/{prescreen_total}",
        f"Scoring agreement (tier + band): {score_hits}/{score_total}",
        "",
        "| case | prescreen exp | actual | pass | tier exp | actual | skill | pref | score |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        p_mark = "pass" if r["prescreen_pass"] else "FAIL"
        s_mark = "" if r["score_pass"] is None else ("pass" if r["score_pass"] else "WARN")
        lines.append(
            f"| {r['slug']} | {r['expected_prescreen']} | {r['actual_prescreen']} | "
            f"{p_mark} | {r['expected_tier'] or ''} | {r['actual_tier'] or ''} | "
            f"{r['skill_fit'] or ''} | {r['preference_alignment'] or ''} | {s_mark} |")
    out.write_text("\n".join(lines) + "\n")
    print(f"\nPrescreen agreement: {prescreen_hits}/{prescreen_total}")
    print(f"Scoring agreement: {score_hits}/{score_total}")
    print(f"Results written to {out}")
    return 0 if prescreen_hits == prescreen_total else 1


if __name__ == "__main__":
    raise SystemExit(main())
