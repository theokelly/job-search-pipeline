"""Discovery stage: fetch new postings from all sources, pre-filter, upsert.

Sources: LinkedIn guest API, Gmail alert ingestion, Greenhouse + Lever ATS
APIs. Rule-based pre-filter runs before anything is saved; JD backfill
fills in descriptions for LinkedIn jobs missing them.

Imports inside functions are lazy so that `job-agent --help` and
short-running commands don't pay the full import cost.
"""
from typing import Callable, Optional

from ._common import (
    PROFILE_PATH_DEFAULT, _default_echo, init_db, get_session, upsert_job, Job,
    logged_run,
)


@logged_run("discover")
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
    from ..config import load_profile
    from ..discovery.greenhouse import fetch_jobs as gh_fetch
    from ..discovery.lever import fetch_jobs as lever_fetch
    from ..filter import pre_filter
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
            from ..discovery.linkedin import fetch_jobs as li_fetch, TIMESPAN_WEEK
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
            from ..discovery.email_alerts import fetch_alert_jobs
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
                from ..discovery.linkedin import backfill_missing_jds
                backfilled = backfill_missing_jds(session)
                if backfilled:
                    session.commit()
                    echo(f"  [linkedin] backfilled JDs for {backfilled} jobs")
            except Exception as e:
                echo(f"  [linkedin] backfill error — {e}")

    return counts, results
