"""
Fan-out discovery: job-first search across the public ATS universe.

The other sources are company-first (enumerate a listed company's board,
filter what comes back). Fan-out inverts that: it searches ATS domains for
title phrases from the profile, so a fitting role at a company that is not
in target_companies still enters the pipeline. Found postings are extracted
through the ATS's own API when the URL is a recognized board posting, and
through manual.fetch_from_url otherwise, then flow into the same
pre-filter / prescreen / scoring stages as every other source.

Design spec: fanout-discovery-design-spec-2026-11-02 (goal workspace).
Provider: Brave Search API via search_provider (profile `fanout:` block).
"""
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from sqlmodel import select

from .. import settings
from ..db import FanoutDeadUrl, Job, get_session
from . import search_provider
from .fetch import (
    PostingGone, fetch_job_from_url, normalize_url, parse_ats_url,
)

# The single-URL extraction cluster moved to discovery/fetch.py (Phase D);
# these names stay importable from here — stages/discover and the fanout
# tests use them through this module.
__all__ = [
    "PostingGone",
    "fetch_job_from_url",
    "normalize_url",
    "parse_ats_url",
    "build_queries",
    "title_matches",
    "fetch_jobs",
]


# ---------------------------------------------------------------------------
# Query construction
# ---------------------------------------------------------------------------

_BRAVE_MAX_WORDS = 50  # Brave rejects queries longer than 50 words / 400 chars


def _phrase_chunks(phrases: list[str], domain: str) -> list[list[str]]:
    """Split phrases so each combined query stays under Brave's word limit."""
    chunks: list[list[str]] = []
    current: list[str] = []
    words = 1  # the site: token
    for phrase in phrases:
        pw = len(phrase.split()) + 1  # phrase words + OR
        if current and words + pw > _BRAVE_MAX_WORDS - 2:
            chunks.append(current)
            current, words = [], 1
        current.append(phrase)
        words += pw
    if current:
        chunks.append(current)
    return chunks


def build_queries(cfg: dict) -> list[str]:
    """Build the run's query list from fanout config.

    Tier 1: one combined OR query per domain (all title phrases).
    Tier 2: one query per phrase for the highest-yield domains
    (cfg phrase_domains x first cfg phrase_count phrases), which ranks
    phrase-exact results higher than the combined query does.
    """
    phrases = [p for p in cfg.get("title_phrases") or [] if p]
    queries: list[str] = []
    for domain in cfg.get("domains") or []:
        for chunk in _phrase_chunks(phrases, domain):
            ors = " OR ".join(f'"{p}"' for p in chunk)
            queries.append(f"site:{domain} ({ors})")
    top = phrases[: int(cfg.get("phrase_count") or 0)]
    for domain in cfg.get("phrase_domains") or []:
        for phrase in top:
            queries.append(f'site:{domain} "{phrase}"')
    return queries


def title_matches(title: str, phrases: list[str]) -> bool:
    """Title gate: does a search result's title carry a target phrase?

    Substring match on the lower-cased title. Ashby-style titles such as
    'Internal AI Enablement Lead @ n8n' still match their phrase.
    """
    t = (title or "").lower()
    return any(p.lower() in t for p in phrases if p)


# ---------------------------------------------------------------------------
# URL identity (dedupe)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Extraction: ATS API first, HTML cascade second
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Fetch gates: page-age ordering + persisted gone verdicts
# ---------------------------------------------------------------------------

def _page_age_dt(value) -> Optional[datetime]:
    """Parse a Brave `page_age` value into a naive UTC datetime.

    Brave returns ISO timestamps (sometimes with a 'Z' or offset); anything
    unparseable is undated (None), which sorts last in fetch order."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _load_dead_urls(ttl_days: int) -> set:
    """Canonical URLs recorded as gone within the TTL. Best-effort: any DB
    problem yields an empty set — the gate must never break discovery."""
    try:
        cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=ttl_days)
        with get_session() as session:
            rows = session.exec(
                select(FanoutDeadUrl).where(FanoutDeadUrl.last_seen >= cutoff)
            ).all()
            return {r.url for r in rows}
    except Exception:
        return set()


def _record_dead_urls(urls: list) -> None:
    """Persist gone verdicts for canonical URLs. Best-effort, like the load."""
    try:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        with get_session() as session:
            for url in urls:
                row = session.get(FanoutDeadUrl, url)
                if row is None:
                    session.add(FanoutDeadUrl(url=url, last_seen=now))
                else:
                    row.last_seen = now
                    session.add(row)
            session.commit()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Main entry point (called from stages/discover.py)
# ---------------------------------------------------------------------------

def fetch_jobs(profile: dict, known_urls: Optional[set] = None) -> tuple[list[Job], dict]:
    """Run fan-out for one discover pass. Returns (jobs, stats).

    Raises EnvironmentError when the search provider is not configured, so
    run_discover can note it and continue with the other sources, matching
    the email path's behavior.

    Candidates are gated before any fetch: canonical URLs in known_urls
    (already in the jobs DB, seeded by run_discover) and URLs recorded as
    gone within the dead-URL TTL are skipped, and the rest are fetched
    freshest-indexed first, so the fetch cap goes to the likeliest-live
    postings.
    """
    cfg = settings.fanout_config(profile)
    provider = (cfg.get("provider") or "brave").lower()
    if provider != "brave":
        raise EnvironmentError(
            f"Unsupported fanout provider '{provider}' — only 'brave' is implemented."
        )
    phrases = cfg.get("title_phrases") or []
    queries = build_queries(cfg)
    stats = {
        "queries": len(queries), "results": 0, "candidates": 0,
        "gated_out": 0, "known_skipped": 0, "dead_skipped": 0,
        "fetched": 0, "gone": 0, "extract_failed": 0, "errors": [],
    }
    known = known_urls or set()
    dead = _load_dead_urls(int(cfg.get("dead_url_ttl_days") or 30))

    # Collect candidate URLs across all queries, deduped by canonical URL.
    candidates: dict[str, dict] = {}
    for query in queries:
        try:
            results = search_provider.search(query, num=int(cfg.get("results_per_query") or 10))
        except EnvironmentError:
            raise
        except Exception as e:
            stats["errors"].append(f"[{query}] {e}")
            continue
        stats["results"] += len(results)
        for r in results:
            if not r.get("url"):
                continue
            canon = normalize_url(r["url"])
            existing = candidates.get(canon)
            # Keep the freshest copy of a posting's metadata: Brave can
            # return the same URL from several queries with different
            # crawl ages, and the freshest copy drives fetch ordering.
            if existing is None or (
                (_page_age_dt(r.get("page_age")) or datetime.min)
                > (_page_age_dt(existing.get("page_age")) or datetime.min)
            ):
                candidates[canon] = r
    stats["candidates"] = len(candidates)

    max_urls = int(cfg.get("max_candidate_urls") or 200)
    max_fetches = int(cfg.get("max_fetches") or 60)
    jobs: list[Job] = []
    gone_urls: list[str] = []
    # Freshest-indexed first: the fetch cap should be spent on postings
    # the index saw recently; undated results go last.
    ordered = sorted(
        candidates.items(),
        key=lambda kv: _page_age_dt(kv[1].get("page_age")) or datetime.min,
        reverse=True,
    )
    for canon, result in ordered[:max_urls]:
        if canon in known:
            stats["known_skipped"] += 1
            continue
        if canon in dead:
            stats["dead_skipped"] += 1
            continue
        title = result.get("title", "")
        if title and not title_matches(title, phrases):
            stats["gated_out"] += 1
            continue
        if stats["fetched"] >= max_fetches:
            break
        stats["fetched"] += 1
        gone = False
        try:
            job = fetch_job_from_url(result["url"])
        except PostingGone:
            stats["gone"] += 1
            gone = True
            gone_urls.append(canon)
            job = None
        except httpx.HTTPStatusError as e:
            # A 404 from the ATS's own API means the posting was live when
            # the search index crawled it and has since closed. That is
            # expected churn for an index-derived source, so it is counted
            # separately instead of surfacing as one error per posting.
            if e.response is not None and e.response.status_code == 404:
                stats["gone"] += 1
                gone = True
                gone_urls.append(canon)
            else:
                stats["errors"].append(f"[{result['url']}] {e}")
            job = None
        except Exception as e:
            stats["errors"].append(f"[{result['url']}] {e}")
            job = None
        if job is None or not job.title or not job.description_raw:
            if not gone:
                stats["extract_failed"] += 1
            continue
        # Provenance is the fan-out source, whatever mechanism extracted it.
        job.source = "search_fanout"
        job.url = canon
        jobs.append(job)
    if gone_urls:
        _record_dead_urls(gone_urls)
    return jobs, stats
