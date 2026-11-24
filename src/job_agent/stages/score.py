"""Scoring stage orchestration: prescreen (Haiku) then tier scoring (Sonnet).

Cluster-aware: only the canonical posting per cluster gets an LLM call;
decisions and scores replicate to siblings. The pieces live in focused
modules — prescreen_filter.py (the gate), planning.py (what gets a
call), score_batch.py (batch submit/fetch) — run_score wires them.
"""
from typing import Callable, Optional

from ._common import (
    PROFILE_PATH_DEFAULT, NARRATIVE_PATH, _default_echo, _replicate_score,
    init_db, get_session, Job, Score, User,
    settings, logged_run,
)
from .planning import _plan_scoring
from .prescreen_filter import _prescreen_filter
from .score_batch import _score_batch


@logged_run("score")
def run_score(
    user_slug: str = "theo",
    limit: Optional[int] = None,
    prescreen: bool = True,
    rescore: bool = False,
    use_batch: Optional[bool] = None,
    profile_path: str = PROFILE_PATH_DEFAULT,
    echo: Callable[[str], None] = _default_echo,
    rescore_tiers: Optional[list[str]] = None,
) -> list:
    """Score all unscored active jobs for the given user.

    rescore: when True, also re-score jobs that already have a Score row.
        Existing scores are NOT copied in this mode; each cluster gets a
        fresh LLM call (a new Score row; readers use the latest per job).
    rescore_tiers: with rescore, limit to active jobs whose LATEST score is
        in one of these tiers (e.g. ["Strong Fit", "Worth Reviewing"]).
    use_batch: None = use the profile yaml's `batches.enabled_by_default`.
        True = batch (50% discount, may wait up to 10 min). False = sync.

    Returns list of (job, score) tuples.
    """
    from ..config import load_profile, load_text
    from ..scoring.score import score_job
    from sqlmodel import select

    profile = load_profile(profile_path)
    narrative = load_text(NARRATIVE_PATH)
    if use_batch is None:
        use_batch = bool(settings.batches_config(profile)["enabled_by_default"])

    init_db()

    scored: list = []
    with get_session() as session:
        db_user = session.exec(select(User).where(User.slug == user_slug)).first()
        if not db_user:
            raise ValueError(f"User '{user_slug}' not found. Run `job-agent init-user` first.")

        scored_ids: set[str] = set(
            row for row in session.exec(
                select(Score.job_id).where(Score.user_id == db_user.id)
            ).all()
        )

        jobs = session.exec(select(Job).where(Job.status == "active")).all()
        if rescore:
            candidates = jobs
            if rescore_tiers:
                latest_tier: dict[str, str] = {}
                for sc in session.exec(
                    select(Score).where(Score.user_id == db_user.id).order_by(Score.scored_at.desc())
                ).all():
                    latest_tier.setdefault(sc.job_id, sc.tier)
                candidates = [j for j in jobs if latest_tier.get(j.id) in rescore_tiers]
            echo(f"  [score] --rescore: considering {len(candidates)} active jobs"
                 + (f" (latest tier in {rescore_tiers})" if rescore_tiers else ""))
        else:
            candidates = [j for j in jobs if j.id not in scored_ids]
        if limit:
            candidates = candidates[:limit]

        if prescreen:
            candidates = _prescreen_filter(
                session, db_user.id, candidates, profile,
                use_batch=use_batch, echo=echo,
            )

        # Drop jobs with empty/short JDs — can't meaningfully score them
        with_jd = []
        for job in candidates:
            if len(job.description_raw.strip()) < 100:
                echo(f"  SKIP (no JD) {job.company} — {job.title}")
                continue
            with_jd.append(job)

        if not with_jd:
            return scored

        # Cluster + check for existing sibling scores (copy if any sibling already scored)
        canonicals_to_score, replications, copy_outputs = _plan_scoring(
            session, db_user.id, with_jd, profile, echo, ignore_existing=rescore
        )
        scored.extend(copy_outputs)

        if not canonicals_to_score:
            return scored

        if use_batch and len(canonicals_to_score) > 1:
            scored.extend(_score_batch(
                session, db_user.id, canonicals_to_score, replications,
                profile, narrative, echo,
            ))
        else:
            for canonical in canonicals_to_score:
                try:
                    s = score_job(canonical, db_user.id, profile, narrative)
                    # Persist canonical + replicate to siblings
                    for sib in replications[canonical.id]:
                        copy = s if sib.id == canonical.id else _replicate_score(s, sib.id)
                        session.add(copy)
                        scored.append((sib, copy))
                    session.commit()
                except Exception as e:
                    echo(f"  ERROR scoring {canonical.company} — {canonical.title}: {e}")

    return scored


