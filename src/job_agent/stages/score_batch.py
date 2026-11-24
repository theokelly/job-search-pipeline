"""Batch glue for the score stage (moved from stages/score.py, Phase E).

_score_batch submits canonical jobs through the Batches API with a
sync fallback; fetch_score_batch / fetch_prescreen_batch persist the
results of a detached batch (`job-agent batch-fetch`). The request
builders and result collectors themselves live in job_agent.batches.
"""
from datetime import datetime, timezone
from typing import Callable

from ._common import (
    PROFILE_PATH_DEFAULT, NARRATIVE_PATH, _default_echo, _replicate_score, init_db, get_session, get_or_create_meta, Job, Score, User, clustering,
)


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
        for canonical, score in collect_score_results(
                batch_id, jobs_by_id, user_id, profile=profile, narrative_guidance=narrative):
            siblings = replications.get(canonical.id, [canonical])
            for sib in siblings:
                copy = score if sib.id == canonical.id else _replicate_score(score, sib.id)
                session.add(copy)
                scored.append((sib, copy))
        session.commit()
        return scored
    except BatchTimeout as e:
        echo(f"  [score] batch timed out. Resume with: job-agent batch-fetch {e.batch_id} --kind score")
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
    """Fetch results from a previously-submitted score batch. Returns # scored.

    Replicates each canonical's score to its cluster siblings, mirroring the
    sync path (previously batch-fetch left siblings unscored).
    """
    from ..config import load_profile, load_text
    from ..batches import iter_results
    from ..scoring.score import _score_from_tool_input, missing_score_fields, score_job
    from sqlmodel import select

    profile = load_profile(profile_path)
    narrative = load_text(NARRATIVE_PATH)
    init_db()
    saved = 0
    scored: list[tuple[Job, Score]] = []
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
            if tool_use is None or missing_score_fields(tool_use.input):
                # Auto tool_choice can't guarantee a complete payload; retry
                # through the sync path (validates + retries). A failure
                # leaves the job unscored for the next run.
                try:
                    score = score_job(job, db_user.id, profile, narrative)
                except Exception:
                    continue
            else:
                score = _score_from_tool_input(job, db_user.id, tool_use.input)
            session.add(score)
            scored.append((job, score))
            saved += 1
        if scored:
            all_jobs = session.exec(select(Job)).all()
            by_key: dict[str, list[Job]] = {}
            for j in all_jobs:
                by_key.setdefault(clustering.cluster_key(j), []).append(j)
            scored_ids = {job.id for job, _ in scored}
            replicated = 0
            for job, score in scored:
                for sib in by_key.get(clustering.cluster_key(job), []):
                    if sib.id not in scored_ids:
                        session.add(_replicate_score(score, sib.id))
                        scored_ids.add(sib.id)
                        replicated += 1
            if replicated:
                echo(f"  [batch-fetch] replicated scores to {replicated} cluster sibling(s)")
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

    load_profile(profile_path)  # validate the profile; the fetch itself needs only the batch
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
            meta.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
            session.add(meta)
            saved += 1
        session.commit()
    echo(f"  [batch-fetch] saved {saved} prescreen decisions from {batch_id}")
    return saved
