"""Build evals/golden_cases.json from the live jobs DB.

The golden set is a frozen snapshot: JD text + the expected prescreen
verdict (and, for scoring anchors, the expected tier with reference
subscores) for a small curated set of real postings. Snapshots are frozen
so the eval stays hermetic — postings expire and the DB moves on, the
cases do not.

The generated JSON contains real JD text and company names, so it is
gitignored (see .gitignore). This builder is the committed artifact: it
references jobs by id prefix only. Rebuild with:

    .venv/bin/python scripts/build_golden_cases.py

Reference subscores are the curated anchors recorded when each case was
admitted (a known-good production score), not "latest at build time" —
the eval measures drift from a fixed point.
"""
import json
from datetime import datetime, timezone
from pathlib import Path

from sqlmodel import select

from job_agent.db import init_db, get_session, Job

# slug, job id prefix, expected prescreen, expected tier, ref skill, ref pref
SPECS = [
    ("case_01", "fa22174d", "approved", "Strong Fit", 82, 95),   # Adyen Head of FDE - the headline false reject
    ("case_02", "c8dcedb7", "approved", "Strong Fit", 85, 90),   # Adyen FDE Team Lead - scored correctly all along
    ("case_03", "6c16b922", "approved", "Strong Fit", 80, 92),   # Adyen AI Transformation Manager - Finance
    ("case_04", "13321b93", "approved", "Long Shot", 28, 62),    # Prosus Group PM, AI Platform
    ("case_05", "BUNQ",     "approved", "Skip", 22, 40),         # bunq Head of Product - gate fixed, scorer lukewarm
    ("case_06", "d18deb98", "approved", "Strong Fit", 82, 72),  # Genmab - label revised to Strong Fit by Theo 2026-11-23 (scorer + production DB agreed; Nov 6 calibration label superseded)
    ("case_07", "f76d6269", "approved", None, None, None),       # n8n Internal AI Enablement Lead - known open miss
    ("case_08", "7982361d", "rejected", None, None, None),       # Lorien - JD states fluent Dutch essential
    ("case_09", "458bc9cf", "rejected", None, None, None),       # TE Connectivity France hybrid - geography
    ("case_10", "242042e8", "rejected", None, None, None),       # Databricks FDE IC - pure engineering delivery
    ("case_11", "3ad2ee80", "rejected", None, None, None),       # Adyen Senior FDE - IC delivery
    ("case_12", "a1ee65d5", "rejected", None, None, None),       # Prosus Visual Design Intern
]

OUT = Path("evals/golden_cases.json")


def main() -> None:
    init_db()
    with get_session() as session:
        jobs = session.exec(select(Job)).all()
        by_prefix = {}
        for job in jobs:
            by_prefix.setdefault(job.id[:8], job)
        bunq = next(j for j in jobs if j.company.lower() == "bunq"
                    and j.title.strip().lower() == "head of product")
        by_prefix["BUNQ"] = bunq

        cases = []
        for slug, prefix, prescreen, tier, ref_skill, ref_pref in SPECS:
            job = by_prefix[prefix]
            if not job.description_raw:
                raise SystemExit(f"{slug}: job {job.id[:8]} has no JD text")
            cases.append({
                "slug": slug,
                "company": job.company,
                "title": job.title,
                "location": job.location,
                "url": job.url,
                "jd_text": job.description_raw,
                "expected_prescreen": prescreen,
                "expected_tier": tier,
                "ref_skill": ref_skill,
                "ref_pref": ref_pref,
            })
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({
        "version": 1,
        "built_at": datetime.now(timezone.utc).isoformat(),
        "cases": cases,
    }, indent=2, ensure_ascii=False))
    print(f"wrote {OUT} with {len(cases)} cases")


if __name__ == "__main__":
    main()
