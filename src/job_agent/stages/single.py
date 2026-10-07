"""Single-job helpers used by the Streamlit UI's on-demand buttons.

Synchronous paths (no batching): score_one always inserts a fresh Score row;
generate_one writes materials and an Application row for one job.
"""
from datetime import datetime, timezone
from pathlib import Path

from ._common import (
    PROFILE_PATH_DEFAULT, NARRATIVE_PATH, init_db, get_session,
    get_or_create_meta, Job, Score, Application, User, UserJobMeta,
)


def score_one(job_id: str, user_id: int, profile_path: str = PROFILE_PATH_DEFAULT) -> Score:
    """Synchronous, single-job rescore. Streamlit's manual refresh entry point —
    always inserts a fresh Score row (intentional)."""
    from ..config import load_profile, load_text
    from ..scoring.score import score_job

    profile = load_profile(profile_path)
    narrative = load_text(NARRATIVE_PATH)
    init_db()
    with get_session() as session:
        job = session.get(Job, job_id)
        s = score_job(job, user_id, profile, narrative)
        session.add(s)
        # Mark prescreen as manual_override so it bypasses future prescreen passes
        meta = get_or_create_meta(session, job_id, user_id)
        meta.prescreen_status = "manual_override"
        meta.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
        session.add(meta)
        session.commit()
        session.refresh(s)
    return s


def generate_one(job_id: str, user_id: int, profile_path: str = PROFILE_PATH_DEFAULT) -> Path:
    """Generate materials for a single job (used by Streamlit)."""
    from ..config import load_profile, load_profile_files
    from ..generation.cover_letter import generate_cover_letter
    from ..generation.resume_recommendation import generate_resume_recommendation
    from ..output import write_outputs
    from ..tailor import budgets_from_markdown
    from sqlmodel import select

    profile = load_profile(profile_path)
    profile_files = load_profile_files(profile=profile)

    init_db()
    with get_session() as session:
        db_user = session.get(User, user_id)
        user_slug = db_user.slug if db_user else "theo"
        job = session.get(Job, job_id)
        s = session.exec(
            select(Score).where(Score.job_id == job_id, Score.user_id == user_id)
            .order_by(Score.scored_at.desc())
        ).first()
        resume_key = f"resume_versions/{s.recommended_resume_version}.md"
        if not profile_files.get(resume_key):
            resume_key = "resume_versions/ai_focused.md"
        resume_text = profile_files.get(resume_key, "")
        cover_letter = generate_cover_letter(job, s, profile_files, profile)
        resume_rec = generate_resume_recommendation(
            job, s, resume_text, profile, budgets_from_markdown(resume_text))
        out_dir = write_outputs(job, s, cover_letter, resume_rec, user_slug=user_slug)
        app = Application(job_id=job_id, user_id=user_id, materials_path=str(out_dir))
        session.add(app)
        session.commit()
    return out_dir
