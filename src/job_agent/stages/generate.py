"""Generation stage: cover letter + resume recommendation (Opus).

Runs for Strong Fit by default (configurable tiers). Cluster-aware: generates
once per cluster for the canonical posting and links sibling Applications
to the same materials directory.
"""
from pathlib import Path
from typing import Callable

from ._common import (
    PROFILE_PATH_DEFAULT, _default_echo, init_db, get_session,
    Job, Score, Application, User, clustering, logged_run,
)


@logged_run("generate")
def run_generate(
    user_slug: str = "theo",
    tiers: str = "Strong Fit",
    profile_path: str = PROFILE_PATH_DEFAULT,
    echo: Callable[[str], None] = _default_echo,
) -> list:
    """Generate materials for all scored-but-unmaterialized jobs. Returns list
    of (job, score, out_dir) tuples."""
    from ..config import load_profile, load_profile_files
    from ..generation.cover_letter import generate_cover_letter
    from ..generation.resume_recommendation import generate_resume_recommendation
    from ..output import write_outputs
    from ..tailor import budgets_from_markdown
    from sqlmodel import select

    target_tiers = [t.strip() for t in tiers.split(",")]
    profile = load_profile(profile_path)
    profile_files = load_profile_files(profile=profile)

    init_db()
    outputs: list = []

    with get_session() as session:
        db_user = session.exec(
            select(User).where(User.slug == user_slug)
        ).first()
        if not db_user:
            raise ValueError(f"User '{user_slug}' not found.")

        existing_app_job_ids = set(
            row for row in session.exec(
                select(Application.job_id).where(Application.user_id == db_user.id)
            ).all()
        )

        all_scores = session.exec(
            select(Score).where(
                Score.user_id == db_user.id,
            ).order_by(Score.scored_at.desc())
        ).all()
        # Deduplicate to the most recent score per job FIRST, then filter by
        # tier. (Filtering first let an old Strong Fit score win after a
        # rescore had downgraded the job.)
        seen_jobs: set = set()
        deduped: list = []
        for s in all_scores:
            if s.job_id not in seen_jobs:
                seen_jobs.add(s.job_id)
                if s.tier in target_tiers:
                    deduped.append(s)
        pending = [s for s in deduped if s.job_id not in existing_app_job_ids]
        if not pending:
            return outputs

        # Group pending by cluster_key. For each cluster:
        #   - if any sibling already has an Application, reuse its materials_path
        #     for the new siblings (no LLM call, just create more Applications)
        #   - otherwise generate once for the canonical and link siblings' Applications
        #     to the same materials_path
        pending_by_cluster: dict[str, list] = {}
        pending_jobs_by_score_id: dict[int, Job] = {}
        for s in pending:
            job = session.get(Job, s.job_id)
            # Only active postings get materials (OVERVIEW: "Only active jobs
            # flow through scoring, generation, and the UI"). Also keeps
            # out_of_scope / expired / closed jobs from burning Opus calls.
            if not job or job.status != "active":
                continue
            pending_jobs_by_score_id[id(s)] = job
            pending_by_cluster.setdefault(clustering.cluster_key(job), []).append(s)

        all_apps = session.exec(select(Application).where(Application.user_id == db_user.id)).all()
        existing_by_cluster: dict[str, str] = {}  # cluster_key → materials_path (most recent)
        for app in all_apps:
            j = session.get(Job, app.job_id)
            if j:
                existing_by_cluster.setdefault(clustering.cluster_key(j), app.materials_path)

        reused = 0
        generated_clusters = 0
        for key, scores in pending_by_cluster.items():
            existing_path = existing_by_cluster.get(key)
            if existing_path:
                # Reuse path; just create Application rows pointing at it
                for s in scores:
                    job = pending_jobs_by_score_id[id(s)]
                    app = Application(job_id=job.id, user_id=db_user.id, materials_path=existing_path)
                    session.add(app)
                    outputs.append((job, s, Path(existing_path)))
                    reused += 1
                continue

            # Pick the score whose Job is the canonical of this cluster (so the JD used for
            # generation is the best-located variant). All scores in this cluster share
            # tier/rationale already due to replication, so we just pick the canonical Job.
            jobs_in_cluster = [pending_jobs_by_score_id[id(s)] for s in scores]
            canonical = clustering.pick_canonical(jobs_in_cluster, profile)
            canonical_score = next(s for s in scores if pending_jobs_by_score_id[id(s)].id == canonical.id)

            resume_key = f"resume_versions/{canonical_score.recommended_resume_version}.md"
            if not profile_files.get(resume_key):
                resume_key = "resume_versions/ai_focused.md"
            resume_text = profile_files.get(resume_key, "")

            try:
                cover_letter = generate_cover_letter(canonical, canonical_score, profile_files, profile)
                resume_rec = generate_resume_recommendation(
                    canonical, canonical_score, resume_text, profile,
                    budgets_from_markdown(resume_text))
                out_dir = write_outputs(canonical, canonical_score, cover_letter, resume_rec, user_slug=user_slug)
                generated_clusters += 1

                # One Application per sibling, all pointing at the canonical's materials dir
                for s in scores:
                    job = pending_jobs_by_score_id[id(s)]
                    app = Application(job_id=job.id, user_id=db_user.id, materials_path=str(out_dir))
                    session.add(app)
                    outputs.append((job, s, out_dir))
            except Exception as e:
                echo(f"  ERROR generating {canonical.company} — {canonical.title}: {e}")
        session.commit()

        if reused:
            echo(f"  [generate] reused existing materials for {reused} new sibling(s)")
        if generated_clusters and generated_clusters < len(pending_by_cluster):
            echo(f"  [generate] generated for {generated_clusters} cluster(s); {len(pending)} pending Application rows total")

    return outputs
