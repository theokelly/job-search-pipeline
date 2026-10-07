"""Housekeeping stages: expire stale postings, prune closed LinkedIn postings.

These keep the pool honest between discovery runs. Only `active` jobs flow
through scoring, generation, and the UI.
"""
from datetime import datetime, timedelta
from typing import Callable, Optional

from ._common import (
    PROFILE_PATH_DEFAULT, _default_echo, init_db, get_session, Job, settings,
)


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
    from ..discovery.linkedin import is_linkedin_url, is_posting_closed
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


def run_sweep(
    days: Optional[int] = None,
    profile_path: str = PROFILE_PATH_DEFAULT,
    echo: Callable[[str], None] = _default_echo,
) -> int:
    """Flip jobs not re-seen in `days` to status='expired'. Returns count expired.

    Reads default `days` from `pipeline.expire_after_days` in profile yaml.
    """
    from ..config import load_profile
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
