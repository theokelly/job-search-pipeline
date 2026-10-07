"""Scoring stage: prescreen (Haiku) then tier scoring (Sonnet).

Cluster-aware: only the canonical posting per cluster gets an LLM call;
decisions and scores replicate to siblings. Supports batch mode (Batches
API, 50% discount) with sync fallback, plus detached batch resume via
fetch_score_batch / fetch_prescreen_batch.
"""
from datetime import datetime
from typing import Callable, Optional

from ._common import (
    PROFILE_PATH_DEFAULT, NARRATIVE_PATH, _default_echo, _replicate_score,
    init_db, get_session, get_or_create_meta, Job, Score, User, UserJobMeta,
    settings, clustering,
)


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


def _plan_scoring(session, user_id: int, with_jd: list, profile: dict, echo: Callable[[str], None],
                  ignore_existing: bool = False):
    """Group with_jd by cluster_key. For each cluster:
      - If any sibling already has a Score, copy it to the unscored jobs in this cluster (no LLM call)
      - Otherwise queue the canonical for an LLM call; replication targets = all unscored siblings

    Returns:
      canonicals_to_score: list of canonical Jobs that need an LLM call
      replications: dict canonical.id → list of Jobs (canonical + siblings) to receive the score
      copy_outputs: list of (Job, Score) already persisted via copy-from-existing path
    """
    from sqlmodel import select

    groups = clustering.group_by_cluster(with_jd)
    canonicals_to_score: list = []
    replications: dict[str, list] = {}
    copy_outputs: list = []
    copied_clusters = 0

    # Build the cluster_key → jobs map for the WHOLE table once (previously a
    # full-table scan ran inside the per-cluster loop). Needed because
    # previously-scored siblings were filtered out upstream.
    all_jobs_by_key: dict[str, list] = {}
    for j in session.exec(select(Job)).all():
        all_jobs_by_key.setdefault(clustering.cluster_key(j), []).append(j)

    for key, unscored_siblings in groups.items():
        if ignore_existing:
            # Rescore mode: never reuse an old score (previously --rescore
            # silently copied the existing score back, so nothing changed).
            canonical = clustering.pick_canonical(unscored_siblings, profile)
            canonicals_to_score.append(canonical)
            replications[canonical.id] = unscored_siblings
            continue
        # Look for an existing Score on ANY sibling in the cluster (incl. ones not in with_jd).
        existing_score = None
        # First check among unscored_siblings themselves (cheap)
        for j in unscored_siblings:
            sc = session.exec(
                select(Score).where(Score.job_id == j.id, Score.user_id == user_id)
                .order_by(Score.scored_at.desc())
            ).first()
            if sc:
                existing_score = sc
                break
        # Then widen: any sibling (active or not) in the DB with this cluster_key
        if existing_score is None:
            unscored_ids = {u.id for u in unscored_siblings}
            matches = [j for j in all_jobs_by_key.get(key, []) if j.id not in unscored_ids]
            for j in matches:
                sc = session.exec(
                    select(Score).where(Score.job_id == j.id, Score.user_id == user_id)
                    .order_by(Score.scored_at.desc())
                ).first()
                if sc:
                    existing_score = sc
                    break

        if existing_score:
            for j in unscored_siblings:
                copy = _replicate_score(existing_score, j.id)
                session.add(copy)
                copy_outputs.append((j, copy))
            copied_clusters += 1
        else:
            canonical = clustering.pick_canonical(unscored_siblings, profile)
            canonicals_to_score.append(canonical)
            replications[canonical.id] = unscored_siblings

    if copy_outputs:
        session.commit()
        echo(f"  [score] copied existing score to {len(copy_outputs)} new siblings across {copied_clusters} cluster(s) — no LLM call")
    if len(canonicals_to_score) < len(groups):
        delta = len(with_jd) - len(canonicals_to_score)
        echo(f"  [score] clustered {len(with_jd)} jobs → {len(canonicals_to_score)} scoring calls (saved {delta})")
    return canonicals_to_score, replications, copy_outputs


def _prescreen_filter(
    session,
    user_id: int,
    candidates: list,
    profile: dict,
    use_batch: bool,
    echo: Callable[[str], None],
) -> list:
    """Run prescreen (batch or sync) and return the approved subset. Persists
    prescreen status to UserJobMeta either way."""
    from ..scoring.prescreen import prescreen_job
    from sqlmodel import select

    # Respect any existing prescreen decision:
    #   manual_override / approved → include without re-running
    #   rejected → skip without re-running
    #   None → still need to prescreen
    needs_prescreen = []
    auto_approved = []
    pre_rejected = 0
    for job in candidates:
        existing_meta = session.exec(
            select(UserJobMeta).where(
                UserJobMeta.job_id == job.id,
                UserJobMeta.user_id == user_id,
            )
        ).first()
        existing_status = existing_meta.prescreen_status if existing_meta else None
        if existing_status in ("manual_override", "approved"):
            auto_approved.append(job)
        elif existing_status == "rejected":
            pre_rejected += 1  # already-rejected; drop silently
        else:
            needs_prescreen.append(job)
    if pre_rejected:
        echo(f"  [prescreen] skipped {pre_rejected} jobs already rejected on a prior run")

    # Cluster needs_prescreen → only submit the canonical of each cluster.
    # Decisions replicate to siblings (same role, different geo broadcast).
    groups = clustering.group_by_cluster(needs_prescreen)
    canonicals: list = []
    canonical_to_siblings: dict[str, list] = {}
    for key, jobs_in_cluster in groups.items():
        canonical = clustering.pick_canonical(jobs_in_cluster, profile)
        canonicals.append(canonical)
        canonical_to_siblings[canonical.id] = jobs_in_cluster  # includes canonical itself
    if len(canonicals) < len(needs_prescreen):
        echo(f"  [prescreen] clustered {len(needs_prescreen)} jobs → {len(canonicals)} prescreen calls")

    canonical_decisions: dict[str, tuple[bool, str]] = {}  # canonical.id → (approved, reason)

    if use_batch and len(canonicals) > 1:
        from ..batches import submit_prescreen_batch, poll_until_ended, collect_prescreen_results, BatchTimeout
        echo(f"  [prescreen] submitting batch for {len(canonicals)} jobs...")
        try:
            batch_id, jobs_by_id = submit_prescreen_batch(canonicals, profile)
            echo(f"  [prescreen] batch_id={batch_id}; polling...")
            poll_until_ended(batch_id, profile, on_tick=lambda s: echo(f"    [batch] {s}"))
            for job, approved, reason in collect_prescreen_results(batch_id, jobs_by_id):
                canonical_decisions[job.id] = (approved, reason)
        except BatchTimeout as e:
            echo(f"  [prescreen] batch timed out. Resume later with: job-agent batch-fetch {e.batch_id} --kind prescreen")
            return auto_approved
        except Exception as e:
            echo(f"  [prescreen] batch error, falling back to sync: {e}")
            for canonical in canonicals:
                approved, reason = prescreen_job(canonical, profile)
                canonical_decisions[canonical.id] = (approved, reason)
    else:
        for canonical in canonicals:
            approved, reason = prescreen_job(canonical, profile)
            canonical_decisions[canonical.id] = (approved, reason)

    # Persist decisions — replicate canonical's decision to all siblings
    approved_jobs = []
    for canonical_id, siblings in canonical_to_siblings.items():
        approved, reason = canonical_decisions.get(canonical_id, (False, "no decision"))
        for job in siblings:
            meta = get_or_create_meta(session, job.id, user_id)
            meta.prescreen_status = "approved" if approved else "rejected"
            meta.prescreen_reason = reason
            meta.updated_at = datetime.utcnow()
            session.add(meta)
            if approved:
                approved_jobs.append(job)
        if not approved:
            canonical = next(j for j in siblings if j.id == canonical_id)
            echo(f"  [prescreen] SKIP {canonical.company} — {canonical.title} ({reason})")
    session.commit()
    final = auto_approved + approved_jobs
    echo(f"  [prescreen] {len(final)}/{len(candidates)} jobs passed filter")
    return final


def _score_batch(
    session,
    user_id: int,
    canonicals: list,
    replications: dict[str, list],
    profile: dict,
    narrative: str,
    echo: Callable[[str], None],
) -> list:
    """Score canonical jobs via batches; replicate each result to its cluster siblings.

    canonicals: list of canonical Jobs (one per cluster) to actually send to the LLM.
    replications: dict canonical.id → list of Jobs (canonical + siblings) to receive the score.

    Falls back to sync per-canonical on batch error.
    """
    from ..scoring.score import score_job
    from ..batches import submit_score_batch, poll_until_ended, collect_score_results, BatchTimeout

    echo(f"  [score] submitting batch for {len(canonicals)} jobs...")
    try:
        batch_id, jobs_by_id = submit_score_batch(canonicals, profile, narrative)
        echo(f"  [score] batch_id={batch_id}; polling...")
        poll_until_ended(batch_id, profile, on_tick=lambda s: echo(f"    [batch] {s}"))
        scored: list = []
        for canonical, score in collect_score_results(batch_id, jobs_by_id, user_id):
            siblings = replications.get(canonical.id, [canonical])
            for sib in siblings:
                copy = score if sib.id == canonical.id else _replicate_score(score, sib.id)
                session.add(copy)
                scored.append((sib, copy))
        session.commit()
        return scored
    except BatchTimeout as e:
        echo(f"  [score] batch timed out. Resume with: job-agent batch-fetch {e.batch_id} --kind score")
        echo(f"  [score] (note: batch-fetch will not auto-replicate to siblings; rerun `job-agent score` after fetch to populate)")
        return []
    except Exception as e:
        echo(f"  [score] batch error, falling back to sync: {e}")
        scored = []
        for canonical in canonicals:
            try:
                s = score_job(canonical, user_id, profile, narrative)
                for sib in replications.get(canonical.id, [canonical]):
                    copy = s if sib.id == canonical.id else _replicate_score(s, sib.id)
                    session.add(copy)
                    scored.append((sib, copy))
                session.commit()
            except Exception as e2:
                echo(f"  ERROR scoring {canonical.company} — {canonical.title}: {e2}")
        return scored


def fetch_score_batch(
    batch_id: str,
    user_slug: str = "theo",
    profile_path: str = PROFILE_PATH_DEFAULT,
    echo: Callable[[str], None] = _default_echo,
) -> int:
    """Fetch results from a previously-submitted score batch. Returns # scored."""
    from ..config import load_profile
    from ..batches import iter_results
    from ..scoring.score import _score_from_tool_input
    from sqlmodel import select

    profile = load_profile(profile_path)
    init_db()
    saved = 0
    with get_session() as session:
        db_user = session.exec(select(User).where(User.slug == user_slug)).first()
        if not db_user:
            raise ValueError(f"User '{user_slug}' not found.")
        for entry in iter_results(batch_id):
            if entry.result.type != "succeeded":
                continue
            # custom_id is f"score_{job.id}"
            job_id = entry.custom_id.removeprefix("score_")
            job = session.get(Job, job_id)
            if not job:
                continue
            tool_use = None
            for block in entry.result.message.content:
                if getattr(block, "type", None) == "tool_use":
                    tool_use = block
                    break
            if tool_use is None:
                continue
            score = _score_from_tool_input(job, db_user.id, tool_use.input)
            session.add(score)
            saved += 1
        session.commit()
    echo(f"  [batch-fetch] saved {saved} scores from {batch_id}")
    return saved


def fetch_prescreen_batch(
    batch_id: str,
    user_slug: str = "theo",
    profile_path: str = PROFILE_PATH_DEFAULT,
    echo: Callable[[str], None] = _default_echo,
) -> int:
    """Fetch results from a previously-submitted prescreen batch. Persists meta."""
    from ..config import load_profile
    from ..batches import iter_results
    from ..scoring.prescreen import parse_prescreen_response
    from sqlmodel import select

    profile = load_profile(profile_path)
    init_db()
    saved = 0
    with get_session() as session:
        db_user = session.exec(select(User).where(User.slug == user_slug)).first()
        if not db_user:
            raise ValueError(f"User '{user_slug}' not found.")
        for entry in iter_results(batch_id):
            if entry.result.type != "succeeded":
                continue
            job_id = entry.custom_id.removeprefix("prescreen_")
            job = session.get(Job, job_id)
            if not job:
                continue
            text = ""
            for block in entry.result.message.content:
                if getattr(block, "type", None) == "text":
                    text = block.text
                    break
            approved, reason = parse_prescreen_response(text)
            meta = get_or_create_meta(session, job_id, db_user.id)
            meta.prescreen_status = "approved" if approved else "rejected"
            meta.prescreen_reason = reason
            meta.updated_at = datetime.utcnow()
            session.add(meta)
            saved += 1
        session.commit()
    echo(f"  [batch-fetch] saved {saved} prescreen decisions from {batch_id}")
    return saved
