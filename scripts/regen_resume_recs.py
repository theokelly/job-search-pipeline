"""
One-off: rewrite resume_recommendation.md for every Application using the
fixed multi-paragraph formatter. Run from /workspace via:

    .venv/bin/python scripts/regen_resume_recs.py
"""
from pathlib import Path
from sqlmodel import select

from job_agent.db import init_db, get_session, Application, Job, Score
from job_agent.config import load_profile, load_profile_files
from job_agent.generation.resume_recommendation import generate_resume_recommendation


def main() -> None:
    profile = load_profile("profile/theo.yaml")
    profile_files = load_profile_files()

    init_db()
    fixed = skipped = failed = 0
    with get_session() as s:
        apps = s.exec(select(Application)).all()
        total = len(apps)
        for i, app in enumerate(apps, 1):
            job = s.get(Job, app.job_id)
            if not job:
                print(f"  [{i}/{total}] SKIP (no job) {app.job_id}", flush=True)
                skipped += 1
                continue

            sc = s.exec(
                select(Score)
                .where(Score.job_id == app.job_id, Score.user_id == app.user_id)
                .order_by(Score.scored_at.desc())
            ).first()
            if not sc:
                print(f"  [{i}/{total}] SKIP (no score) {job.company} — {job.title}", flush=True)
                skipped += 1
                continue

            target_dir = Path(app.materials_path)
            if not target_dir.exists():
                print(f"  [{i}/{total}] SKIP (no dir) {target_dir}", flush=True)
                skipped += 1
                continue

            resume_key = f"resume_versions/{sc.recommended_resume_version}.md"
            if not profile_files.get(resume_key):
                resume_key = "resume_versions/ai_focused.md"
            resume_text = profile_files.get(resume_key, "")

            try:
                md = generate_resume_recommendation(job, sc, resume_text, profile)
                (target_dir / "resume_recommendation.md").write_text(md)
                fixed += 1
                print(f"  [{i}/{total}] OK {job.company} — {job.title}", flush=True)
            except Exception as e:
                failed += 1
                print(f"  [{i}/{total}] ERROR {job.company} — {job.title}: {e}", flush=True)

    print(f"\nDone: {fixed} regenerated, {skipped} skipped, {failed} failed")


if __name__ == "__main__":
    main()
