"""Prescreen filter for the score stage (moved from stages/score.py, Phase E).

Runs the rule pre-filter's survivors through the Haiku prescreen —
inline for small sets, via the Batches API for large ones — and
persists each decision to UserJobMeta. Kept separate from run_score
so the gate logic can be read (and tested) without the scoring
orchestration around it.
"""
from datetime import datetime, timezone
from typing import Callable

from ._common import (
    get_or_create_meta, UserJobMeta, clustering,
)


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
            meta.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
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
