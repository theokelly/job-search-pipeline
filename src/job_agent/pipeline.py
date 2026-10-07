"""
Pipeline orchestration: discover → prescreen → score → generate, plus the
stale-posting sweep. All pipeline logic lives here so that both the CLI and the
Streamlit UI can call the same code paths.

Imports inside functions are lazy (mirrors the previous cli.py shape) so that
`job-agent --help` and short-running commands don't pay the full import cost.
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

from .db import (
    init_db, get_session, upsert_job, get_or_create_meta,
    Job, Score, Application, User, UserJobMeta,
)
from . import settings
from . import clustering


PROFILE_PATH_DEFAULT = "profile/theo.yaml"
NARRATIVE_PATH = "profile/narrative_guidance.md"

# Default echo writes to stdout. Streamlit / tests pass their own echo callable.
# Kept dependency-free so this module loads in the streamlit Python (no typer there).
def _default_echo(msg: str, err: bool = False) -> None:
    stream = sys.stderr if err else sys.stdout
    print(msg, file=stream)


# ---------------------------------------------------------------------------
# Replication helpers — same-cluster siblings share decisions / materials
# ---------------------------------------------------------------------------

def _replicate_score(original: Score, target_job_id: str) -> Score:
    """Return a new Score row pointing at target_job_id, with all other fields copied."""
    return Score(
        job_id=target_job_id,
        user_id=original.user_id,
        tier=original.tier,
        skill_fit=original.skill_fit,
        preference_alignment=original.preference_alignment,
        recommended_resume_version=original.recommended_resume_version,
        gap_assessment=original.gap_assessment,
        positioning_notes=original.positioning_notes,
        rationale=original.rationale,
        raw_response=original.raw_response,
        scored_at=original.scored_at,
    )


# ---------------------------------------------------------------------------
# Discover
# ---------------------------------------------------------------------------

def run_discover(
    dry_run: bool = False,
    priority: Optional[str] = None,
    include_email: bool = False,
    email_days: int = 14,
    include_linkedin: bool = False,
    refetch_jds: bool = False,
    profile_path: str = PROFILE_PATH_DEFAULT,
    echo: Callable[[str], None] = _default_echo,
) -> tuple[dict, list]:
    """
    Fetch jobs from all configured ATS sources, apply pre-filter, upsert to DB.
    Returns (counts, results).

    refetch_jds: when True, ignore the DB's known-ids set and re-fetch every JD
    on the LinkedIn + email paths. Use this after a fetching bug fix.
    """
    from .config import load_profile
    from .discovery.greenhouse import fetch_jobs as gh_fetch
    from .discovery.lever import fetch_jobs as lever_fetch
    from .filter import pre_filter
    from sqlmodel import select as sql_select

    profile = load_profile(profile_path)
    init_db()

    companies = profile.get("target_companies", [])
    if priority:
        companies = [c for c in companies if c.get("priority") == priority]

    counts = {"found": 0, "passed": 0, "new": 0, "updated": 0, "skipped": 0}
    results: list = []

    # Build known_ids set once — shared by LinkedIn and email paths
    known_ids: set[str] = set()
    if include_linkedin or include_email:
        with get_session() as _s:
            known_ids = {
                j.id for j in _s.exec(
                    sql_select(Job).where(Job.description_raw != "")
                ).all()
            }

    # LinkedIn guest API search (keyword-based, fetches full JDs)
    if include_linkedin:
        try:
            from .discovery.linkedin import fetch_jobs as li_fetch, TIMESPAN_WEEK
            li_queries = profile.get("linkedin_search_queries", [])
            if not li_queries:
                echo("  [linkedin] no linkedin_search_queries in profile yaml; skipping")
            else:
                echo(f"  [linkedin] searching {len(li_queries)} queries...")
                li_jobs = li_fetch(
                    li_queries,
                    pages_per_query=2,
                    timespan=TIMESPAN_WEEK,
                    known_ids=known_ids,
                    refetch_jds=refetch_jds,
                )
                echo(f"  [linkedin] fetched {len(li_jobs)} jobs")
                for job in li_jobs:
                    counts["found"] += 1
                    keep, reason = pre_filter(job, profile)
                    if not keep:
                        counts["skipped"] += 1
                        results.append(("skip", job, reason))
                    else:
                        counts["passed"] += 1
                        results.append(("keep", job, None))
        except Exception as e:
            echo(f"  [linkedin] error — {e}")

    # Email alert ingestion (only when Gmail is configured)
    if include_email:
        try:
            from .discovery.email_alerts import fetch_alert_jobs
            email_jobs = fetch_alert_jobs(
                since_days=email_days,
                known_ids=known_ids,
                refetch_jds=refetch_jds,
            )
            echo(f"  [email] fetched {len(email_jobs)} alert jobs")
            for job in email_jobs:
                counts["found"] += 1
                keep, reason = pre_filter(job, profile)
                if not keep:
                    counts["skipped"] += 1
                    results.append(("skip", job, reason))
                else:
                    counts["passed"] += 1
                    results.append(("keep", job, None))
        except EnvironmentError as e:
            echo(f"  [email] skipped — {e}")
        except Exception as e:
            echo(f"  [email] error — {e}")

    for company in companies:
        name = company.get("name", "")
        gh_token = company.get("greenhouse_token")
        lever_slug = company.get("lever_slug")

        if not gh_token and not lever_slug:
            continue

        jobs = []
        if gh_token:
            try:
                jobs += gh_fetch(name, gh_token)
            except Exception as e:
                echo(f"  [greenhouse] {name}: {e}")
        if lever_slug:
            try:
                jobs += lever_fetch(name, lever_slug)
            except Exception as e:
                echo(f"  [lever] {name}: {e}")

        for job in jobs:
            counts["found"] += 1
            keep, reason = pre_filter(job, profile)
            if not keep:
                counts["skipped"] += 1
                results.append(("skip", job, reason))
                continue

            counts["passed"] += 1
            results.append(("keep", job, None))

    if not dry_run:
        with get_session() as session:
            for action, job, _ in results:
                if action == "keep":
                    is_new = upsert_job(session, job)
                    if is_new:
                        counts["new"] += 1
                    else:
                        counts["updated"] += 1
            session.commit()

            # Backfill JDs for any LinkedIn jobs still missing descriptions
            try:
                from .discovery.linkedin import backfill_missing_jds
                backfilled = backfill_missing_jds(session)
                if backfilled:
                    session.commit()
                    echo(f"  [linkedin] backfilled JDs for {backfilled} jobs")
            except Exception as e:
                echo(f"  [linkedin] backfill error — {e}")

    return counts, results


# ---------------------------------------------------------------------------
# Sweep (stale postings)
# ---------------------------------------------------------------------------

def run_sweep(
    days: Optional[int] = None,
    profile_path: str = PROFILE_PATH_DEFAULT,
    echo: Callable[[str], None] = _default_echo,
) -> int:
    """Flip jobs not re-seen in `days` to status='expired'. Returns count expired.

    Reads default `days` from `pipeline.expire_after_days` in profile yaml.
    """
    from .config import load_profile
    from sqlmodel import select

    profile = load_profile(profile_path)
    if days is None:
        days = int(settings.pipeline_config(profile)["expire_after_days"])

    init_db()
    cutoff = datetime.utcnow() - timedelta(days=days)
    expired = 0
    with get_session() as session:
        stale = session.exec(
            select(Job).where(Job.status == "active", Job.last_seen < cutoff)
        ).all()
        for job in stale:
            job.status = "expired"
            session.add(job)
            expired += 1
        if expired:
            session.commit()
    echo(f"  [sweep] expired {expired} jobs (last_seen < {cutoff.date()})")
    return expired


# ---------------------------------------------------------------------------
# Prune closed LinkedIn postings
# ---------------------------------------------------------------------------

class _RateLimiter:
    """Thread-safe minimum-interval throttle shared across worker threads.

    Spaces outbound requests by `min_interval` seconds *globally*, regardless of
    how many workers are running, so concurrency speeds up wall-clock without
    multiplying LinkedIn's request rate past what it tolerated single-threaded.
    """

    def __init__(self, min_interval: float):
        import threading
        self._min_interval = max(0.0, min_interval)
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> None:
        if self._min_interval <= 0:
            return
        import time as _time
        with self._lock:
            now = _time.monotonic()
            sleep_for = self._next - now
            self._next = max(now, self._next) + self._min_interval
        if sleep_for > 0:
            _time.sleep(sleep_for)


def run_prune_closed(
    limit: Optional[int] = None,
    max_workers: int = 8,
    min_interval: float = 0.25,
    recheck_after_hours: float = 24.0,
    skip_fresh_hours: float = 24.0,
    echo: Callable[[str], None] = _default_echo,
) -> int:
    """Check active LinkedIn postings for a 'No longer accepting applications'
    warning and flip those to status='closed' so they drop out of the pipeline
    (scoring, generation, and the UI all filter on status=='active').

    Returns the number of jobs pruned. A job is only pruned when the page is
    fetched AND shows the warning — transient fetch failures (is_posting_closed
    -> None) leave the job untouched and leave `last_checked` unset so they're
    retried next run.

    Speed/efficiency controls:
      max_workers         — concurrent fetches (network-bound; DB writes stay on
                            this thread since SQLite sessions aren't thread-safe).
      min_interval        — global seconds between outbound requests (rate cap).
      skip_fresh_hours    — skip postings re-seen by discovery within this window:
                            still appearing in search means still live, so there's
                            nothing to prune.
      recheck_after_hours — skip postings already checked within this window so a
                            run only re-fetches postings whose state may have
                            changed, instead of all active LinkedIn jobs.
    """
    from concurrent.futures import ThreadPoolExecutor
    from .discovery.linkedin import is_linkedin_url, is_posting_closed
    from sqlmodel import select

    init_db()
    now = datetime.utcnow()
    fresh_cutoff = now - timedelta(hours=skip_fresh_hours)
    recheck_cutoff = now - timedelta(hours=recheck_after_hours)

    pruned = 0
    with get_session() as session:
        active = session.exec(select(Job).where(Job.status == "active")).all()
        candidates = [
            j for j in active
            if is_linkedin_url(j.url)
            and j.last_seen < fresh_cutoff
            and (j.last_checked is None or j.last_checked < recheck_cutoff)
        ]
        # Oldest / never-checked first so a --limit run makes the most useful progress.
        candidates.sort(key=lambda j: j.last_checked or datetime.min)
        total_li = sum(1 for j in active if is_linkedin_url(j.url))
        if limit:
            candidates = candidates[:limit]

        echo(
            f"  [prune] {total_li} active LinkedIn postings; "
            f"{len(candidates)} due for a check (skipped fresh / recently-checked)..."
        )
        if not candidates:
            echo("  [prune] nothing to check")
            return 0

        limiter = _RateLimiter(min_interval)

        def _check(job: Job):
            limiter.wait()
            return job, is_posting_closed(job.url, request_delay=0.0)

        checked = 0
        with ThreadPoolExecutor(max_workers=max(1, max_workers)) as pool:
            for job, closed in pool.map(_check, candidates):
                checked += 1
                if closed is None:
                    # Unknown (fetch failed / rate limited): leave last_checked
                    # unset so this posting is retried on the next run.
                    continue
                job.last_checked = now
                if closed:
                    job.status = "closed"
                    pruned += 1
                    echo(f"  [prune] CLOSED {job.company} — {job.title}")
                session.add(job)
                if checked % 25 == 0 and checked != len(candidates):
                    echo(f"  [prune] {checked}/{len(candidates)} checked, {pruned} closed so far")
        session.commit()

    echo(f"  [prune] pruned {pruned} closed LinkedIn posting(s)")
    return pruned


# ---------------------------------------------------------------------------
# Score (with optional batch mode + rescore)
# ---------------------------------------------------------------------------

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
    from .config import load_profile, load_text
    from .scoring.score import score_job
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
    from .scoring.prescreen import prescreen_job
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
        from .batches import submit_prescreen_batch, poll_until_ended, collect_prescreen_results, BatchTimeout
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
    from .scoring.score import score_job
    from .batches import submit_score_batch, poll_until_ended, collect_score_results, BatchTimeout

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


# ---------------------------------------------------------------------------
# Generate (cover letter + resume rec)
# ---------------------------------------------------------------------------

def run_generate(
    user_slug: str = "theo",
    tiers: str = "Strong Fit",
    profile_path: str = PROFILE_PATH_DEFAULT,
    echo: Callable[[str], None] = _default_echo,
) -> list:
    """Generate materials for all scored-but-unmaterialized jobs. Returns list
    of (job, score, out_dir) tuples."""
    from .config import load_profile, load_profile_files
    from .generation.cover_letter import generate_cover_letter
    from .generation.resume_recommendation import generate_resume_recommendation
    from .output import write_outputs
    from sqlmodel import select

    target_tiers = [t.strip() for t in tiers.split(",")]
    profile = load_profile(profile_path)
    profile_files = load_profile_files()

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
                resume_rec = generate_resume_recommendation(canonical, canonical_score, resume_text, profile)
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


# ---------------------------------------------------------------------------
# Batch fetch (resume a detached batch)
# ---------------------------------------------------------------------------

def fetch_score_batch(
    batch_id: str,
    user_slug: str = "theo",
    profile_path: str = PROFILE_PATH_DEFAULT,
    echo: Callable[[str], None] = _default_echo,
) -> int:
    """Fetch results from a previously-submitted score batch. Returns # scored."""
    from .config import load_profile
    from .batches import iter_results
    from .scoring.score import _score_from_tool_input
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
    from .config import load_profile
    from .batches import iter_results
    from .scoring.prescreen import parse_prescreen_response
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


# ---------------------------------------------------------------------------
# Single-job helpers (used by Streamlit "score now" / "generate now")
# ---------------------------------------------------------------------------

def score_one(job_id: str, user_id: int, profile_path: str = PROFILE_PATH_DEFAULT) -> Score:
    """Synchronous, single-job rescore. Streamlit's manual refresh entry point —
    always inserts a fresh Score row (intentional)."""
    from .config import load_profile, load_text
    from .scoring.score import score_job

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
        meta.updated_at = datetime.utcnow()
        session.add(meta)
        session.commit()
        session.refresh(s)
    return s


def generate_one(job_id: str, user_id: int, profile_path: str = PROFILE_PATH_DEFAULT) -> Path:
    """Generate materials for a single job (used by Streamlit)."""
    from .config import load_profile, load_profile_files
    from .generation.cover_letter import generate_cover_letter
    from .generation.resume_recommendation import generate_resume_recommendation
    from .output import write_outputs
    from sqlmodel import select

    profile = load_profile(profile_path)
    profile_files = load_profile_files()

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
        resume_rec = generate_resume_recommendation(job, s, resume_text, profile)
        out_dir = write_outputs(job, s, cover_letter, resume_rec, user_slug=user_slug)
        app = Application(job_id=job_id, user_id=user_id, materials_path=str(out_dir))
        session.add(app)
        session.commit()
    return out_dir
