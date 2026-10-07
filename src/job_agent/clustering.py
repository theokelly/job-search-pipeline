"""
Multi-geo posting consolidation.

Many postings (Jobgether, Scopely, HelloFresh, Databricks) are the same role
broadcast across 4-6 geographies. They have distinct LinkedIn job IDs and
distinct DB rows but represent one logical opportunity — Theo applies once.

This module derives a `cluster_key` from (company, title) so the pipeline can:
  - score one canonical job per cluster (saves $$$ on LLM calls)
  - replicate the result onto sibling rows (UI / queries keep working)
  - generate materials once per cluster (one materials_path shared)

Pure functions over Job rows — no DB writes — to keep things testable.
"""
from typing import Iterable

from .db import Job
from . import settings


# ---------------------------------------------------------------------------
# Key derivation
# ---------------------------------------------------------------------------

def _normalize(s: str) -> str:
    """Lowercase, keep only [a-z0-9]. Strips punctuation, whitespace, accents
    don't matter since we just want a stable key."""
    return "".join(ch for ch in (s or "").lower() if ch.isalnum())


def cluster_key(job: Job) -> str:
    """Returns a key that groups together job postings sharing company + title.

    Location is intentionally excluded — that's exactly what we want to collapse
    across (e.g. "Director of Data" posted in NL/UK/Spain/Portugal).
    """
    return f"{_normalize(job.company)}|{_normalize(job.title)}"


# ---------------------------------------------------------------------------
# Canonical selection
# ---------------------------------------------------------------------------

# Geographic priority — lower wins. Source of truth: Theo's stated ordering
# (updated 2026-10, after the move to Amsterdam; London no longer in scope):
#   Amsterdam commuting range > Remote > rest of NL > on-site rest of EU > other
# Commuting range comes from profile.geography.commutable_terms; profiles
# without it fall back to the NL term set.
_GEO_COMMUTE = 0
_GEO_REMOTE = 1
_GEO_NL_OTHER = 2
_GEO_ONSITE_EU = 3
_GEO_OTHER = 4

_NL_TERMS = {"netherlands", "nederland", "amsterdam", "rotterdam", "eindhoven", "utrecht"}


def geo_priority(job: Job, profile: dict) -> int:
    """Classify a job's location into a priority bucket. Lower = more preferred."""
    loc = (job.location or "").lower()
    if not loc:
        return _GEO_OTHER

    geo = profile.get("geography") or {}
    commute_terms = {t.lower() for t in geo.get("commutable_terms") or []} or _NL_TERMS
    remote_terms = {t.lower() for t in geo.get("remote_location_signals") or []} or {"remote", "hybrid"}

    if any(t in loc for t in commute_terms):
        return _GEO_COMMUTE
    if any(t in loc for t in remote_terms):
        return _GEO_REMOTE
    if any(t in loc for t in _NL_TERMS):
        return _GEO_NL_OTHER
    if any(t in loc for t in settings.eu_place_terms(profile)):
        return _GEO_ONSITE_EU
    return _GEO_OTHER


def pick_canonical(jobs: list[Job], profile: dict) -> Job:
    """Pick the canonical sibling. Sort order:
        1. geo_priority (Amsterdam commute > remote > rest of NL > other EU > other)
        2. JD completeness (longer description_raw wins) — avoids picking a
           LinkedIn card with no JD when a sibling has the full posting
        3. first_seen (most recent wins as a final tiebreaker)
    """
    return min(
        jobs,
        key=lambda j: (
            geo_priority(j, profile),
            -len(j.description_raw or ""),
            -(j.first_seen.timestamp() if j.first_seen else 0),
        ),
    )


# ---------------------------------------------------------------------------
# Grouping
# ---------------------------------------------------------------------------

def group_by_cluster(jobs: Iterable[Job]) -> dict[str, list[Job]]:
    """Group jobs by cluster_key. Singletons get their own one-element list."""
    groups: dict[str, list[Job]] = {}
    for j in jobs:
        groups.setdefault(cluster_key(j), []).append(j)
    return groups


def canonical_and_siblings(jobs: list[Job], profile: dict) -> tuple[Job, list[Job]]:
    """Return (canonical, siblings) where siblings excludes the canonical."""
    canonical = pick_canonical(jobs, profile)
    siblings = [j for j in jobs if j.id != canonical.id]
    return canonical, siblings
