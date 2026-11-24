"""Scoring planner for the score stage (moved from stages/score.py, Phase E).

Decides what actually gets an LLM call: groups candidates by cluster,
replicates existing scores to siblings where allowed, copies outputs
for exact re-postings, and returns the canonical jobs still needing a
score. Pure decision logic over the session — no API calls.
"""
from typing import Callable

from ._common import (
    _replicate_score, Job, Score, clustering,
)


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
