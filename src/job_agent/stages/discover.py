"""Discovery stage: fetch new postings from all sources, pre-filter, upsert.

Sources: LinkedIn guest API, Gmail alert ingestion, Greenhouse + Lever +
Ashby + Workday ATS APIs, and search fan-out (job-first, any company, via
--fanout). Rule-based pre-filter runs before anything is saved; JD backfill
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
    include_fanout: bool = False,
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
    from ..discovery.ashby import fetch_jobs as ashby_fetch
    from ..discovery.workday import fetch_jobs as wd_fetch
    from ..filter import pre_filter
    from sqlmodel import select as sql_select

    profile = load_profile(profile_path)
    init_db()

    companies = profile.get("target_companies", [])
    if priority:
        companies = [c for c in companies if c.get("priority") == priority]

    counts = {"found": 0, "passed": 0, "new": 0, "updated": 0, "skipped": 0, "dupes": 0, "errors": 0}
    results: list = []
    error_details: list[str] = []

    def _note_error(context: str, e: Exception) -> None:
        """Echo a fetch failure now AND remember it for the final summary."""
        detail = f"[{context}] {e}"
        echo(f"  {detail}")
        counts["errors"] += 1
        error_details.append(detail)

    # Build known_ids set once — shared by LinkedIn and email paths
    known_ids: set[str] = set()
    if include_linkedin or include_email:
        with get_session() as _s:
            known_ids = {
                j.id for j in _s.exec(
                    sql_select(Job).where(Job.description_raw != "")
                ).all()
            }

    # Canonical-URL set for cross-source dedupe. Fan-out jobs are identified
    # by URL first (their company/title/location strings can differ from the
    # board copy of the same posting, which would defeat the job_id hash).
    # Seeded from the DB; every source's keeps claim their URL so a later
    # source in the same run cannot re-add the same posting.
    seen_urls: set[str] = set()
    fanout_kept: list = []
    _canon_url = None
    if include_fanout:
        from ..discovery.fanout import normalize_url as _canon_url_fn

        _canon_url = _canon_url_fn
        with get_session() as _s:
            seen_urls = {_canon_url(j.url) for j in _s.exec(sql_select(Job)).all() if j.url}

    def _claim(job) -> bool:
        """True when this job's canonical URL is new; claims it when so."""
        if _canon_url is None:
            return True
        canon = _canon_url(job.url)
        if not canon or canon in seen_urls:
            return not canon
        seen_urls.add(canon)
        return True

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
                    elif not _claim(job):
                        counts["dupes"] += 1
                        counts["skipped"] += 1
                        results.append(("skip", job, "duplicate URL (cross-source)"))
                    else:
                        counts["passed"] += 1
                        results.append(("keep", job, None))
        except Exception as e:
            _note_error("linkedin", e)

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
                elif not _claim(job):
                    counts["dupes"] += 1
                    counts["skipped"] += 1
                    results.append(("skip", job, "duplicate URL (cross-source)"))
                else:
                    counts["passed"] += 1
                    results.append(("keep", job, None))
        except EnvironmentError as e:
            _note_error("email (skipped)", e)
        except Exception as e:
            _note_error("email", e)

    # Search fan-out (job-first: ATS domains x title phrases, any company)
    if include_fanout:
        try:
            from ..discovery.fanout import fetch_jobs as fanout_fetch

            echo("  [fanout] searching ATS domains for profile title phrases...")
            fo_jobs, fo_stats = fanout_fetch(profile, known_urls=seen_urls)
            echo(
                f"  [fanout] {fo_stats['queries']} queries, {fo_stats['results']} results, "
                f"{fo_stats['candidates']} candidates, {fo_stats['gated_out']} gated out, "
                f"{fo_stats['known_skipped']} already known, {fo_stats['dead_skipped']} known dead, "
                f"fetched {fo_stats['fetched']}, {fo_stats['gone']} no longer live, "
                f"{fo_stats['extract_failed']} extraction failures"
            )
            for err in fo_stats.get("errors", []):
                _note_error("fanout", RuntimeError(err))
            for job in fo_jobs:
                counts["found"] += 1
                keep, reason = pre_filter(job, profile)
                if not keep:
                    counts["skipped"] += 1
                    results.append(("skip", job, reason))
                elif not _claim(job):
                    counts["dupes"] += 1
                    counts["skipped"] += 1
                    results.append(("skip", job, "duplicate URL (cross-source)"))
                else:
                    counts["passed"] += 1
                    results.append(("keep", job, None))
                    fanout_kept.append(job)
        except EnvironmentError as e:
            _note_error("fanout (skipped)", e)
        except Exception as e:
            _note_error("fanout", e)

    total_boards = sum(
        1 for c in companies
        if c.get("greenhouse_token") or c.get("lever_slug")
        or c.get("ashby_board") or c.get("workday_board")
    )
    if total_boards:
        echo(f"  [boards] fetching {total_boards} ATS boards...")
    board_i = 0
    for company in companies:
        name = company.get("name", "")
        gh_token = company.get("greenhouse_token")
        lever_slug = company.get("lever_slug")
        ashby_board = company.get("ashby_board")
        workday_board = company.get("workday_board")

        if not gh_token and not lever_slug and not ashby_board and not workday_board:
            continue
        board_i += 1

        jobs = []
        if gh_token:
            try:
                jobs += gh_fetch(name, gh_token)
            except Exception as e:
                _note_error(f"greenhouse {name}", e)
        if lever_slug:
            try:
                jobs += lever_fetch(name, lever_slug)
            except Exception as e:
                _note_error(f"lever {name}", e)
        if ashby_board:
            try:
                jobs += ashby_fetch(name, ashby_board)
            except Exception as e:
                _note_error(f"ashby {name}", e)
        if workday_board:
            try:
                jobs += wd_fetch(name, workday_board)
            except Exception as e:
                _note_error(f"workday {name}", e)

        echo(f"  [boards] {board_i}/{total_boards} {name}: {len(jobs)} jobs")
        for job in jobs:
            counts["found"] += 1
            keep, reason = pre_filter(job, profile)
            if not keep:
                counts["skipped"] += 1
                results.append(("skip", job, reason))
                continue
            if not _claim(job):
                counts["dupes"] += 1
                counts["skipped"] += 1
                results.append(("skip", job, "duplicate URL (cross-source)"))
                continue

            counts["passed"] += 1
            results.append(("keep", job, None))

    # Auto-promotion suggestions (suggest-only: the profile is never edited
    # by a run). A kept fan-out role from an unlisted company is evidence
    # that company's board belongs in target_companies, where the board API
    # gives fresher, complete coverage than the search index.
    if include_fanout and fanout_kept:
        from ..discovery.fanout import parse_ats_url

        listed = {
            (c.get("name") or "").lower()
            for c in profile.get("target_companies", [])
        }
        by_company: dict[str, list] = {}
        for job in fanout_kept:
            if job.company.lower() not in listed:
                by_company.setdefault(job.company, []).append(job)
        for company_name, cjobs in sorted(by_company.items()):
            parsed = parse_ats_url(cjobs[0].url)
            board_note = f" — board: {parsed[0]} {parsed[1]}" if parsed else ""
            echo(
                f"  [fanout] unlisted company: {company_name} "
                f"({len(cjobs)} kept role(s)){board_note} — consider adding "
                f"it to target_companies"
            )

    if not dry_run:
        echo(f"  [discover] saving {counts['passed']} jobs to DB...")
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
                _note_error("linkedin backfill", e)

    counts["error_details"] = error_details
    return counts, results
