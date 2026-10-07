"""
CLI surface — thin Typer wrappers around `pipeline.py`. Keep this file short:
all orchestration lives in pipeline.py so the Streamlit UI can share it.
"""
from typing import Optional

import typer
from dotenv import load_dotenv

app = typer.Typer(help="Job search agent — discover, score, and generate application materials.")


def _bootstrap() -> None:
    """Load env vars. Called at the top of every command."""
    load_dotenv("data/secrets.env", override=False)
    load_dotenv(".env", override=False)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

@app.command()
def discover(
    dry_run: bool = typer.Option(False, "--dry-run", help="Print results without saving to DB"),
    priority: Optional[str] = typer.Option(None, "--priority", help="Only process companies with this priority (high/medium/low)"),
    email: bool = typer.Option(False, "--email", help="Include Gmail alert ingestion"),
    email_days: int = typer.Option(14, "--email-days", help="How many days back to scan alert emails (use 365 for first-time backfill)"),
    linkedin: bool = typer.Option(False, "--linkedin", help="Search LinkedIn via guest API (slower, fetches full JDs)"),
    refetch_jds: bool = typer.Option(False, "--refetch-jds", help="Re-fetch JDs for jobs already in the DB (LinkedIn + email). Use after fixing a fetch bug."),
):
    """Fetch new job postings from Greenhouse and Lever APIs, and optionally Gmail alerts or LinkedIn search."""
    _bootstrap()
    from .pipeline import run_discover
    counts, results = run_discover(
        dry_run=dry_run,
        priority=priority,
        include_email=email,
        email_days=email_days,
        include_linkedin=linkedin,
        refetch_jds=refetch_jds,
    )

    if dry_run:
        for action, job, reason in results:
            if action == "keep":
                typer.echo(f"  + {job.company} | {job.title} | {job.location}")
            else:
                typer.echo(f"  SKIP {job.company} — {job.title} ({reason})")

    typer.echo(
        f"\nDiscover: {counts['found']} found, {counts['passed']} passed filter"
        + (f", {counts['new']} new, {counts['updated']} updated" if not dry_run else "")
    )


@app.command()
def sweep(
    days: Optional[int] = typer.Option(None, "--days", help="Days since last_seen to expire (default from yaml: pipeline.expire_after_days)"),
):
    """Mark jobs not re-seen in `--days` as expired."""
    _bootstrap()
    from .pipeline import run_sweep
    run_sweep(days=days)


@app.command(name="prune-closed")
def prune_closed(
    limit: Optional[int] = typer.Option(None, "--limit", help="Max LinkedIn postings to check"),
    max_workers: int = typer.Option(8, "--max-workers", help="Concurrent LinkedIn fetches"),
    min_interval: float = typer.Option(0.25, "--min-interval", help="Global seconds between requests (avoids 429s)"),
    recheck_after: float = typer.Option(24.0, "--recheck-after", help="Skip postings checked within this many hours"),
    skip_fresh: float = typer.Option(24.0, "--skip-fresh", help="Skip postings re-seen within this many hours (still live)"),
):
    """Mark active LinkedIn jobs showing 'No longer accepting applications' as closed."""
    _bootstrap()
    from .pipeline import run_prune_closed
    run_prune_closed(
        limit=limit, max_workers=max_workers, min_interval=min_interval,
        recheck_after_hours=recheck_after, skip_fresh_hours=skip_fresh,
    )


@app.command()
def score(
    user: str = typer.Option("theo", "--user"),
    limit: Optional[int] = typer.Option(None, "--limit", help="Max jobs to score"),
    prescreen: bool = typer.Option(True, "--prescreen/--no-prescreen", help="Run Haiku pre-filter before Sonnet scoring (default: on)"),
    rescore: bool = typer.Option(False, "--rescore", help="Also re-score jobs that already have a Score (default: skip already-scored)"),
    rescore_tiers: Optional[str] = typer.Option(None, "--rescore-tiers", help='With --rescore: only jobs whose latest tier is in this comma-separated list, e.g. "Strong Fit,Worth Reviewing"'),
    batch: Optional[bool] = typer.Option(None, "--batch/--no-batch", help="Use Anthropic batches (50% discount, may wait up to 10 min). Default from yaml: batches.enabled_by_default"),
):
    """Score unscored active jobs using the configured scoring model."""
    _bootstrap()
    from .pipeline import run_score
    typer.echo(f"Scoring jobs for user '{user}'...")
    results = run_score(
        user_slug=user, limit=limit, prescreen=prescreen,
        rescore=rescore, use_batch=batch,
        rescore_tiers=[t.strip() for t in rescore_tiers.split(",")] if rescore_tiers else None,
    )
    for job, s in results:
        typer.echo(f"  {job.company} — {job.title}")
        typer.echo(f"    → {s.tier} (skill={s.skill_fit}, pref={s.preference_alignment})")
    typer.echo(f"\nScored {len(results)} jobs.")


@app.command()
def generate(
    user: str = typer.Option("theo", "--user"),
    tiers: str = typer.Option("Strong Fit", "--tiers", help='Comma-separated tiers. Default: Strong Fit only — generate Worth Reviewing per-job from the UI, or pass --tiers "Strong Fit,Worth Reviewing"'),
):
    """Generate cover letters and resume recommendations."""
    _bootstrap()
    from .pipeline import run_generate
    typer.echo(f"Generating materials for tiers: {tiers}")
    outputs = run_generate(user_slug=user, tiers=tiers)
    for job, s, out_dir in outputs:
        typer.echo(f"  {job.company} — {job.title} ({s.tier})")
        typer.echo(f"    → {out_dir}")
    typer.echo(f"\nGenerated materials for {len(outputs)} jobs.")


@app.command()
def run(
    user: str = typer.Option("theo", "--user"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    tiers: str = typer.Option("Strong Fit", "--tiers", help='Comma-separated tiers to auto-generate materials for. Default: Strong Fit only (Worth Reviewing on demand via UI)'),
    prescreen: bool = typer.Option(True, "--prescreen/--no-prescreen", help="Run Haiku pre-filter before Sonnet scoring (default: on)"),
    rescore: bool = typer.Option(False, "--rescore", help="Also re-score jobs that already have a Score"),
    linkedin: bool = typer.Option(False, "--linkedin", help="Search LinkedIn via guest API"),
    email: bool = typer.Option(False, "--email", help="Include Gmail alert ingestion"),
    email_days: int = typer.Option(14, "--email-days", help="How many days back to scan alert emails"),
    refetch_jds: bool = typer.Option(False, "--refetch-jds", help="Re-fetch JDs for jobs already in the DB"),
    batch: Optional[bool] = typer.Option(None, "--batch/--no-batch", help="Use Anthropic batches for prescreen + score"),
):
    """Run the full pipeline: sweep → discover → score → generate."""
    _bootstrap()
    from .config import load_profile
    from . import settings
    from .pipeline import run_discover, run_sweep, run_score, run_generate, run_prune_closed

    profile = load_profile("profile/theo.yaml")
    pipe_cfg = settings.pipeline_config(profile)

    if pipe_cfg.get("auto_sweep_on_run", True) and not dry_run:
        typer.echo("=== Step 0/3: Sweep stale postings ===")
        run_sweep()

    typer.echo("=== Step 1/3: Discover ===")
    counts, _ = run_discover(
        dry_run=dry_run, include_linkedin=linkedin, include_email=email,
        email_days=email_days, refetch_jds=refetch_jds,
    )
    typer.echo(
        f"  {counts['found']} found, {counts['passed']} passed filter"
        + (f", {counts['new']} new, {counts['updated']} updated" if not dry_run else "")
    )

    if dry_run:
        typer.echo("\n[dry-run] Skipping score + generate steps.")
        return

    if pipe_cfg.get("prune_closed_on_run", True):
        typer.echo("\n=== Step 1b/3: Prune closed LinkedIn postings ===")
        run_prune_closed()

    typer.echo("\n=== Step 2/3: Score ===")
    scored = run_score(user_slug=user, prescreen=prescreen, rescore=rescore, use_batch=batch)
    for job, s in scored:
        typer.echo(f"  {job.company} — {job.title} → {s.tier}")
    typer.echo(f"  Scored {len(scored)} jobs.")

    typer.echo("\n=== Step 3/3: Generate ===")
    outputs = run_generate(user_slug=user, tiers=tiers)
    for job, s, out_dir in outputs:
        typer.echo(f"  {job.company} — {job.title} ({s.tier}) → {out_dir}")
    typer.echo(f"  Generated materials for {len(outputs)} jobs.")

    typer.echo("\nPipeline complete.")


@app.command(name="batch-fetch")
def batch_fetch(
    batch_id: str = typer.Argument(..., help="Batch ID printed by an earlier --batch run"),
    kind: str = typer.Option("score", "--kind", help="'score' or 'prescreen'"),
    user: str = typer.Option("theo", "--user"),
):
    """Pull results from a previously-submitted batch and persist them."""
    _bootstrap()
    from .pipeline import fetch_score_batch, fetch_prescreen_batch
    if kind == "score":
        fetch_score_batch(batch_id, user_slug=user)
    elif kind == "prescreen":
        fetch_prescreen_batch(batch_id, user_slug=user)
    else:
        raise typer.BadParameter("--kind must be 'score' or 'prescreen'")


@app.command(name="init-user")
def init_user(
    slug: str = typer.Option("theo", "--slug"),
    profile_path: str = typer.Option("profile/theo.yaml", "--profile"),
):
    """Create the user record in the database (run once at setup)."""
    _bootstrap()
    from .db import init_db, get_session, User
    from sqlmodel import select

    init_db()
    with get_session() as session:
        existing = session.exec(select(User).where(User.slug == slug)).first()
        if existing:
            typer.echo(f"User '{slug}' already exists (id={existing.id}).")
            return
        u = User(slug=slug, profile_path=profile_path)
        session.add(u)
        session.commit()
        session.refresh(u)
        typer.echo(f"Created user '{slug}' (id={u.id}).")


@app.command()
def status(
    user: str = typer.Option("theo", "--user"),
):
    """Show pipeline status: job counts, tier breakdown, last activity."""
    _bootstrap()
    from .db import init_db, get_session, Job, Score, User, Application
    from sqlmodel import select, func

    init_db()
    with get_session() as session:
        db_user = session.exec(select(User).where(User.slug == user)).first()

        total_jobs = session.exec(select(func.count(Job.id))).one()
        active_jobs = session.exec(
            select(func.count(Job.id)).where(Job.status == "active")
        ).one()
        expired_jobs = session.exec(
            select(func.count(Job.id)).where(Job.status == "expired")
        ).one()
        closed_jobs = session.exec(
            select(func.count(Job.id)).where(Job.status == "closed")
        ).one()

        typer.echo(f"\n{'='*50}")
        typer.echo(f"  Job Agent Status — user: {user}")
        typer.echo(f"{'='*50}")
        typer.echo(f"  Jobs in DB:  {total_jobs} total, {active_jobs} active, {expired_jobs} expired, {closed_jobs} closed")

        if not db_user:
            typer.echo(f"  (User '{user}' not found — run `job-agent init-user` first)")
            return

        # Jobs by company
        company_counts = session.exec(
            select(Job.company, func.count(Job.id))
            .where(Job.status == "active")
            .group_by(Job.company)
            .order_by(func.count(Job.id).desc())
        ).all()
        typer.echo(f"\n  Active jobs by company:")
        for company, count in company_counts:
            typer.echo(f"    {company:<35} {count:>3}")

        # Scores by tier
        scored_total = session.exec(
            select(func.count(Score.id)).where(Score.user_id == db_user.id)
        ).one()
        typer.echo(f"\n  Scores: {scored_total} total")
        if scored_total:
            tier_counts = session.exec(
                select(Score.tier, func.count(Score.id))
                .where(Score.user_id == db_user.id)
                .group_by(Score.tier)
                .order_by(func.count(Score.id).desc())
            ).all()
            for tier, count in tier_counts:
                typer.echo(f"    {tier:<20} {count:>3}")

        # Duplicate-score check (idempotency signal)
        dup_rows = session.exec(
            select(Score.job_id, func.count(Score.id).label("c"))
            .where(Score.user_id == db_user.id)
            .group_by(Score.job_id)
            .having(func.count(Score.id) > 1)
        ).all()
        if dup_rows:
            redundant = sum(r[1] - 1 for r in dup_rows)
            typer.echo(f"  Duplicate scores: {len(dup_rows)} jobs scored more than once ({redundant} extra calls)")

        # Applications (generated materials)
        app_total = session.exec(
            select(func.count(Application.id)).where(Application.user_id == db_user.id)
        ).one()
        typer.echo(f"\n  Materials generated: {app_total}")

        distinct_scored = len({r for r in session.exec(
            select(Score.job_id).where(Score.user_id == db_user.id)
        ).all()})
        unscored = active_jobs - distinct_scored
        typer.echo(f"  Unscored active jobs: {max(0, unscored)}")
        typer.echo(f"{'='*50}\n")


if __name__ == "__main__":
    app()
