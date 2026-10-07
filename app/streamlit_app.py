"""
Job Pipeline — Streamlit UI

Run with:
  streamlit run app/streamlit_app.py --server.port 8501
"""
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

# Make the workspace importable
sys.path.insert(0, str(Path(__file__).parent.parent))
os.chdir(Path(__file__).parent.parent)

from dotenv import load_dotenv
load_dotenv(".env")

from sqlmodel import select, func
from src.job_agent.db import (
    init_db, get_session, get_or_create_meta,
    Job, Score, Application, User, UserJobMeta,
)
from src.job_agent.pipeline import score_one, generate_one
from src.job_agent.discovery.manual import fetch_from_url
from src.job_agent.db import job_id as compute_job_id, upsert_job
from src.job_agent import clustering
from src.job_agent.config import load_profile

# ---------------------------------------------------------------------------
# Page config
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="Job Pipeline",
    page_icon="🎯",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
TIER_ORDER = {
    "Strong Fit": 0, "Worth Reviewing": 1, "Long Shot": 2, "Skip": 3,
    "Screened Out": 4, "Unscored": 5,
}
TIER_CELL_STYLE = {
    "Strong Fit":     "background-color: #1a472a; color: #6fcf97; font-weight: 600",
    "Worth Reviewing":"background-color: #3d3200; color: #f2c94c; font-weight: 600",
    "Long Shot":      "background-color: #4a2010; color: #eb8c34",
    "Skip":           "background-color: #1e1e1e; color: #666",
    "Screened Out":   "background-color: #1a1a2e; color: #555; font-style: italic",
    "Unscored":       "",
}
APP_STATUS_LABELS = {
    None: "—",
    "applied": "✉️ Applied",
    "interviewing": "💬 Interviewing",
    "offer": "🎉 Offer",
    "rejected": "❌ Rejected",
    "not_pursuing": "✕ Not Pursuing",
}

# Posting lifecycle status (Job.status), distinct from application status above.
POSTING_STATUS_LABELS = {
    "active": "🟢 Active",
    "closed": "🔴 Closed",
    "expired": "⚪ Expired",
    "out_of_scope": "⊘ Out of scope",
}

# Sentinel used in the Status multiselect to represent rows with no assigned status.
# Kept distinct from None so it can travel through Streamlit widget state cleanly.
NO_STATUS_SENTINEL = "__none__"
SOURCE_LABELS = {
    "linkedin_search": "LinkedIn Search",
    "linkedin": "LinkedIn Alert",
    "greenhouse": "Greenhouse",
    "greenhouse_api": "Greenhouse",
    "lever": "Lever",
    "indeed": "Indeed",
    "glassdoor": "Glassdoor",
}

# ---------------------------------------------------------------------------
# API-usage estimation
# ---------------------------------------------------------------------------
# The pipeline does NOT persist real token counts, so the Stats page shows an
# ESTIMATE: actual per-stage call volumes (from the DB) × the model each stage
# uses × published list pricing × per-call token assumptions. It is a spend
# ballpark, not a billed figure. The assumptions below are surfaced in an
# expander on the Stats page so they can be sanity-checked.

# Published list price, USD per 1M tokens.
MODEL_PRICING = {
    "claude-haiku-4-5-20251001": {"in": 1.0,  "out": 5.0,  "label": "Haiku 4.5"},
    "claude-sonnet-4-6":         {"in": 3.0,  "out": 15.0, "label": "Sonnet 4.6"},
    "claude-opus-4-7":           {"in": 15.0, "out": 75.0, "label": "Opus 4.7"},
}
BATCH_DISCOUNT = 0.5  # Anthropic Message Batches API: 50% off input + output.

# Per-call token estimate per stage, derived from the truncation limits in
# src/job_agent/settings.py (≈ chars/4 tokens) plus prompt/profile overhead.
# Generation fires two calls per job: a cover letter (Opus for Strong Fit, else
# Sonnet) and a resume recommendation (Haiku).
STAGE_COST_MODEL = {
    "prescreen":            {"model": "claude-haiku-4-5-20251001", "in": 500,  "out": 40,  "batch": True},
    "score":                {"model": "claude-sonnet-4-6",         "in": 1900, "out": 700, "batch": True},
    "cover_letter_strong":  {"model": "claude-opus-4-7",           "in": 4300, "out": 800, "batch": False},
    "cover_letter_other":   {"model": "claude-sonnet-4-6",         "in": 4300, "out": 800, "batch": False},
    "resume_rec":           {"model": "claude-haiku-4-5-20251001", "in": 1750, "out": 600, "batch": False},
}


def _stage_call_cost(stage: str) -> float:
    """Estimated USD for a single call of a pipeline stage (batch discount applied)."""
    spec = STAGE_COST_MODEL[stage]
    price = MODEL_PRICING[spec["model"]]
    cost = spec["in"] / 1e6 * price["in"] + spec["out"] / 1e6 * price["out"]
    return cost * (BATCH_DISCOUNT if spec["batch"] else 1.0)


# ---------------------------------------------------------------------------
# Location cleanup
# ---------------------------------------------------------------------------
_COUNTRY_WORDS = {
    "netherlands", "nederland", "united kingdom", "england", "scotland", "wales",
    "germany", "france", "belgium", "austria", "spain", "italy", "sweden",
    "denmark", "norway", "finland", "switzerland", "usa", "united states",
    "canada", "australia",
}


def _clean_location(loc: str) -> str:
    """Return a short city label: 'Amsterdam', 'London', 'Remote', 'Multiple', etc."""
    if not loc:
        return "—"
    loc = loc.strip()
    low = loc.lower()

    if "remote" in low:
        return "Remote"

    # Multiple cities separated by slash or " / "
    slash_parts = [p.strip() for p in loc.split("/") if p.strip()]
    if len(slash_parts) > 1:
        cities = {p.split(",")[0].split("·")[0].strip() for p in slash_parts}
        if len(cities) > 1:
            return "Multiple"

    first = slash_parts[0] if slash_parts else loc
    # Take text before first comma, middot, or em dash
    city = first.split(",")[0].split("·")[0].split("–")[0].strip()
    # Strip " Area" suffix
    if city.lower().endswith(" area"):
        city = city[:-5].strip()
    # If the result is a country name, return it as-is (no city specified)
    return city if city else "—"


# ---------------------------------------------------------------------------
# Session state defaults
# ---------------------------------------------------------------------------
def _init_state():
    defaults = {
        "page": "jobs",           # jobs | stats
        "selected_job_id": None,
        "filter_tiers": [],
        "filter_statuses": [],
        "filter_sources": [],
        "filter_cities": [],
        "search_text": "",
        "filter_since": "all",   # all | today | week | custom
        "filter_since_date": None,
        "include_inactive": False,  # also load closed/expired postings

        # Add-Job page state
        "add_fetched": None,        # dict from fetch_from_url, or None before fetch
        "add_url_input": "",
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v

_init_state()


# ---------------------------------------------------------------------------
# DB helpers (cached per run to avoid N+1 queries)
# ---------------------------------------------------------------------------
@st.cache_data(ttl=30)
def load_jobs_df(include_inactive: bool = False):
    """Load all jobs with their latest score and meta into a DataFrame.

    By default only ``active`` postings are loaded. When ``include_inactive`` is
    set, ``closed`` and ``expired`` postings are loaded too — otherwise a job you
    already applied to silently vanishes from the UI once its posting comes down.
    """
    init_db()
    profile = load_profile("profile/theo.yaml")
    with get_session() as s:
        user = s.exec(select(User).where(User.slug == "theo")).first()
        user_id = user.id if user else None

        job_query = select(Job)
        if not include_inactive:
            job_query = job_query.where(Job.status == "active")
        jobs = s.exec(job_query).all()

        # Cluster derivation: mark canonical per (company, title) cluster
        cluster_groups: dict[str, list[Job]] = {}
        for j in jobs:
            cluster_groups.setdefault(clustering.cluster_key(j), []).append(j)
        canonical_id_by_cluster = {
            k: clustering.pick_canonical(rows, profile).id
            for k, rows in cluster_groups.items()
        }

        # Latest score per job
        scores: dict[str, Score] = {}
        if user_id:
            all_scores = s.exec(
                select(Score).where(Score.user_id == user_id).order_by(Score.scored_at.desc())
            ).all()
            for sc in all_scores:
                if sc.job_id not in scores:
                    scores[sc.job_id] = sc

        # Meta per job
        metas: dict[str, UserJobMeta] = {}
        if user_id:
            all_metas = s.exec(
                select(UserJobMeta).where(UserJobMeta.user_id == user_id)
            ).all()
            for m in all_metas:
                metas[m.job_id] = m

        # Application (materials generated) per job
        app_paths: set[str] = set()
        if user_id:
            apps = s.exec(select(Application).where(Application.user_id == user_id)).all()
            for a in apps:
                app_paths.add(a.job_id)

        rows = []
        for job in jobs:
            sc = scores.get(job.id)
            meta = metas.get(job.id)
            prescreen_status = meta.prescreen_status if meta else None
            tier = sc.tier if sc else None
            if tier:
                display_tier = tier
            elif prescreen_status == "rejected":
                display_tier = "Screened Out"
            else:
                display_tier = "Unscored"
            ck = clustering.cluster_key(job)
            rows.append({
                "id": job.id,
                "company": job.company,
                "title": job.title,
                "location": job.location,
                "city": _clean_location(job.location),
                "source": SOURCE_LABELS.get(job.source, job.source),
                "posting_status": job.status,
                "first_seen": job.first_seen.date() if job.first_seen else None,
                "has_jd": len((job.description_raw or "").strip()) >= 100,
                "tier": tier,
                "display_tier": display_tier,
                "skill_fit": sc.skill_fit if sc else None,
                "pref_alignment": sc.preference_alignment if sc else None,
                "scored": sc is not None,
                "has_materials": job.id in app_paths,
                "app_status": meta.app_status if meta else None,
                "prescreen_status": prescreen_status,
                "prescreen_reason": meta.prescreen_reason if meta else None,
                "cluster_key": ck,
                "is_canonical": job.id == canonical_id_by_cluster[ck],
            })

        return pd.DataFrame(rows), user_id


@st.cache_data(ttl=30)
def load_screened_out_df():
    """Load jobs that were rejected by the Haiku prescreen."""
    init_db()
    with get_session() as s:
        user = s.exec(select(User).where(User.slug == "theo")).first()
        if not user:
            return pd.DataFrame(), None

        metas = s.exec(
            select(UserJobMeta).where(
                UserJobMeta.user_id == user.id,
                UserJobMeta.prescreen_status == "rejected",
            )
        ).all()

        rows = []
        for meta in metas:
            job = s.get(Job, meta.job_id)
            if not job:
                continue
            rows.append({
                "id": job.id,
                "company": job.company,
                "title": job.title,
                "location": job.location,
                "source": SOURCE_LABELS.get(job.source, job.source),
                "rejection_reason": (meta.prescreen_reason or "").replace("REJECT - ", "").replace("REJECT", "").strip(" -"),
                "has_jd": len((job.description_raw or "").strip()) >= 100,
            })
        return pd.DataFrame(rows), user.id


@st.cache_data(ttl=30)
def load_stats_timeseries():
    """Temporal data for the Stats-page journey view.

    Deliberately independent of the active/inactive sidebar toggle — the journey
    is the whole search history, so every posting (active, closed, expired) counts.
    Returns a dict of small DataFrames plus the funnel/cost counts.
    """
    import re
    _date_rx = re.compile(r"/(\d{4}-\d{2}-\d{2})_")

    init_db()
    with get_session() as s:
        user = s.exec(select(User).where(User.slug == "theo")).first()
        uid = user.id if user else None

        jobs = s.exec(select(Job)).all()
        scores = s.exec(select(Score).where(Score.user_id == uid)).all() if uid else []
        metas = s.exec(select(UserJobMeta).where(UserJobMeta.user_id == uid)).all() if uid else []
        apps = s.exec(select(Application).where(Application.user_id == uid)).all() if uid else []

        job_by_id = {j.id: j for j in jobs}

        # --- Discovery: one row per posting, keyed by first_seen ---
        discovery = pd.DataFrame([
            {"date": j.first_seen, "source": SOURCE_LABELS.get(j.source, j.source)}
            for j in jobs if j.first_seen
        ])

        # --- Scoring: latest score per job (avoid double-counting re-scores) ---
        latest_score: dict[str, Score] = {}
        for sc in sorted(scores, key=lambda x: x.scored_at or datetime.min, reverse=True):
            if sc.job_id not in latest_score:
                latest_score[sc.job_id] = sc
        scoring = pd.DataFrame([
            {"date": sc.scored_at, "tier": sc.tier}
            for sc in latest_score.values() if sc.scored_at
        ])

        # --- Generation: date parsed from the materials_path prefix ---
        gen_rows = []
        for a in apps:
            m = _date_rx.search(a.materials_path or "")
            if not m:
                continue
            tier = latest_score[a.job_id].tier if a.job_id in latest_score else None
            gen_rows.append({"date": pd.to_datetime(m.group(1)), "tier": tier})
        generation = pd.DataFrame(gen_rows)

        # --- Application journey: current tracked status + when it was set ---
        journey_rows = []
        for m in metas:
            if not m.app_status or not m.app_status_at:
                continue
            job = job_by_id.get(m.job_id)
            sc = latest_score.get(m.job_id)
            journey_rows.append({
                "date": m.app_status_at,
                "app_status": m.app_status,
                "company": job.company if job else "—",
                "title": job.title if job else "—",
                "first_seen": job.first_seen if job else None,
                "tier": sc.tier if sc else None,
            })
        journey = pd.DataFrame(journey_rows)

        # Prescreen decisions (for the funnel; no clean per-decision timestamp).
        # Every screened job costs one Haiku call, approved or not.
        prescreen_approved = sum(
            1 for m in metas if m.prescreen_status in ("approved", "manual_override")
        )
        prescreen_total = sum(1 for m in metas if m.prescreen_status)

    # Normalise date columns to pandas datetimes
    for frame in (discovery, scoring, generation, journey):
        if not frame.empty:
            frame["date"] = pd.to_datetime(frame["date"])

    # --- Funnel: distinct-job counts per stage ---
    strong = int((scoring["tier"] == "Strong Fit").sum()) if not scoring.empty else 0
    worth = int((scoring["tier"] == "Worth Reviewing").sum()) if not scoring.empty else 0
    applied_now = int((journey["app_status"] == "applied").sum()) if not journey.empty else 0
    funnel = [
        ("Discovered",        len(jobs)),
        ("Prescreen passed",  prescreen_approved),
        ("Scored",            len(latest_score)),
        ("Strong / Worth",    strong + worth),
        ("Materials made",    len(generation)),
        ("Actioned",          len(journey)),
        ("Applied",           applied_now),
    ]

    # --- Cost inputs: per-stage call volumes and generation split by tier ---
    gen_strong = int((generation["tier"] == "Strong Fit").sum()) if not generation.empty else 0
    counts = {
        "prescreen": prescreen_total,
        "score": len(latest_score),
        "cover_letter_strong": gen_strong,
        "cover_letter_other": len(generation) - gen_strong,
        "resume_rec": len(generation),
    }

    return {
        "discovery": discovery,
        "scoring": scoring,
        "generation": generation,
        "journey": journey,
        "funnel": funnel,
        "cost_counts": counts,
    }


def load_job_detail(job_id: str):
    """Load full detail for a single job."""
    init_db()
    with get_session() as s:
        user = s.exec(select(User).where(User.slug == "theo")).first()
        user_id = user.id if user else None
        job = s.get(Job, job_id)
        sc = None
        meta = None
        app = None
        if user_id and job:
            all_scores = s.exec(
                select(Score).where(
                    Score.job_id == job_id,
                    Score.user_id == user_id,
                ).order_by(Score.scored_at.desc())
            ).all()
            sc = all_scores[0] if all_scores else None
            meta = s.exec(
                select(UserJobMeta).where(
                    UserJobMeta.job_id == job_id,
                    UserJobMeta.user_id == user_id,
                )
            ).first()
            app = s.exec(
                select(Application).where(
                    Application.job_id == job_id,
                    Application.user_id == user_id,
                )
            ).first()
        return job, sc, meta, app, user_id


def save_job_fields(job_id: str, company: str, title: str, location: str):
    init_db()
    with get_session() as s:
        job = s.get(Job, job_id)
        if job:
            job.company = company.strip()
            job.title = title.strip()
            job.location = location.strip()
            s.add(job)
            s.commit()
    st.cache_data.clear()


def set_app_status(job_id: str, user_id: int, status: str):
    """Update app_status for the clicked job AND every sibling in its cluster.

    Siblings represent the same logical role posted across geographies; the user
    only applies once, so the state should be consistent across all variants.
    """
    init_db()
    with get_session() as s:
        clicked = s.get(Job, job_id)
        if not clicked:
            return
        target_key = clustering.cluster_key(clicked)
        all_jobs = s.exec(select(Job)).all()
        sibling_ids = [j.id for j in all_jobs if clustering.cluster_key(j) == target_key]

        new_status = status if status != "clear" else None
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        for jid in sibling_ids:
            meta = get_or_create_meta(s, jid, user_id)
            meta.app_status = new_status
            meta.app_status_at = now
            meta.updated_at = now
            s.add(meta)
        s.commit()
    st.cache_data.clear()


def score_job_now(job_id: str, user_id: int) -> Score:
    """Manual rescore from the UI — always inserts a fresh Score row by design.

    Delegates to pipeline.score_one which handles meta update + persistence.
    """
    sc = score_one(job_id, user_id)
    st.cache_data.clear()
    return sc


def generate_materials_now(job_id: str, user_id: int) -> Path:
    """Generate cover letter + resume rec for a job immediately."""
    out_dir = generate_one(job_id, user_id)
    st.cache_data.clear()
    return out_dir


def tailor_resume_now(materials_path: str) -> dict:
    """Apply the resume recommendation to a PPTX copy. Returns the tailor report."""
    from src.job_agent.tailor import tailor_from_output_dir
    profile = load_profile("profile/theo.yaml")
    return tailor_from_output_dir(materials_path, profile)


def render_tailor_report(report: dict):
    """Display a tailor report in the UI."""
    from src.job_agent.tailor import format_report
    up = report.get("drive_upload") or {}
    if up.get("web_link"):
        st.success(f"📤 Uploaded to Drive: [{up['filename']}]({up['web_link']})")
    elif up:
        st.success(f"📤 Uploaded to Drive as {up['filename']}")
    if report.get("drive_upload_error"):
        st.error(f"Drive upload failed: {report['drive_upload_error']}")
    n_flag = len(report.get("flagged", []))
    if n_flag:
        st.warning(f"⚠️ {n_flag} edit(s) need your eye — see the report below.")
    with st.expander("Full tailor report", expanded=n_flag > 0):
        st.code(format_report(report), language="text")


def read_material(materials_path: str, filename: str) -> str:
    p = Path(materials_path) / filename
    return p.read_text() if p.exists() else ""


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------
def render_sidebar(df: pd.DataFrame):
    with st.sidebar:
        st.title("🎯 Job Pipeline")
        st.markdown("---")

        # Navigation
        pages = {"jobs": "📋 Jobs", "add": "➕ Add Job", "stats": "📊 Stats"}
        for key, label in pages.items():
            is_active = st.session_state.page == key
            if st.button(label, width="stretch", type="primary" if is_active else "secondary"):
                st.session_state.page = key
                st.session_state.selected_job_id = None
                st.rerun()

        st.markdown("---")

        # Filters (only relevant on jobs page)
        if st.session_state.page == "jobs" and df is not None and len(df):
            st.markdown("**Filters**")

            all_tiers = sorted(df["display_tier"].dropna().unique(), key=lambda t: TIER_ORDER.get(t, 99))
            st.session_state.filter_tiers = st.multiselect(
                "Tier", options=all_tiers, default=st.session_state.filter_tiers
            )

            statuses = [NO_STATUS_SENTINEL, "applied", "interviewing", "offer", "rejected", "not_pursuing"]
            status_labels = {**APP_STATUS_LABELS, NO_STATUS_SENTINEL: "○ No status"}
            st.session_state.filter_statuses = st.multiselect(
                "Status", options=statuses, default=st.session_state.filter_statuses,
                format_func=lambda s: status_labels.get(s, s),
            )

            sources = sorted(df["source"].dropna().unique())
            st.session_state.filter_sources = st.multiselect(
                "Source", options=sources, default=st.session_state.filter_sources,
            )

            # City filter — sorted by frequency so the most common cities are at the top
            city_counts = df["city"].dropna().value_counts()
            cities = list(city_counts.index)
            st.session_state.filter_cities = st.multiselect(
                "City", options=cities, default=st.session_state.filter_cities,
                format_func=lambda c: f"{c} ({city_counts[c]})",
            )

            st.session_state.search_text = st.text_input(
                "Search", value=st.session_state.search_text, placeholder="Company or title..."
            )

            st.session_state.include_inactive = st.checkbox(
                "Include closed / expired postings",
                value=st.session_state.include_inactive,
                help="Also show postings that have come down. Useful for jobs you already applied to.",
            )

            # "New since" filter — supports daily-incremental workflow
            since_options = ["all", "today", "week", "custom"]
            since_labels = {"all": "All jobs", "today": "New today", "week": "New this week", "custom": "Since date…"}
            st.session_state.filter_since = st.radio(
                "Show",
                options=since_options,
                format_func=lambda s: since_labels[s],
                index=since_options.index(st.session_state.filter_since),
                horizontal=False,
            )
            if st.session_state.filter_since == "custom":
                st.session_state.filter_since_date = st.date_input(
                    "Since (first seen)",
                    value=st.session_state.filter_since_date or (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=7)).date(),
                )

            if any([
                st.session_state.filter_tiers,
                st.session_state.filter_statuses,
                st.session_state.filter_sources,
                st.session_state.filter_cities,
                st.session_state.search_text,
                st.session_state.filter_since != "all",
            ]):
                if st.button("Clear filters", width="stretch"):
                    st.session_state.filter_tiers = []
                    st.session_state.filter_statuses = []
                    st.session_state.filter_sources = []
                    st.session_state.filter_cities = []
                    st.session_state.search_text = ""
                    st.session_state.filter_since = "all"
                    st.session_state.filter_since_date = None
                    st.rerun()


# ---------------------------------------------------------------------------
# Job list view
# ---------------------------------------------------------------------------
def apply_filters(df: pd.DataFrame) -> pd.DataFrame:
    if st.session_state.filter_tiers:
        df = df[df["display_tier"].isin(st.session_state.filter_tiers)]
    if st.session_state.filter_statuses:
        chosen = st.session_state.filter_statuses
        include_none = NO_STATUS_SENTINEL in chosen
        real_statuses = [s for s in chosen if s != NO_STATUS_SENTINEL]
        mask = df["app_status"].isin(real_statuses) if real_statuses else pd.Series(False, index=df.index)
        if include_none:
            mask = mask | df["app_status"].isna()
        df = df[mask]
    if st.session_state.filter_sources:
        df = df[df["source"].isin(st.session_state.filter_sources)]
    if st.session_state.filter_cities:
        df = df[df["city"].isin(st.session_state.filter_cities)]
    if st.session_state.search_text:
        q = st.session_state.search_text.lower()
        df = df[
            df["company"].str.lower().str.contains(q, na=False) |
            df["title"].str.lower().str.contains(q, na=False)
        ]

    # "New since" filter — based on first_seen
    since = st.session_state.filter_since
    if since != "all":
        today = datetime.now(timezone.utc).replace(tzinfo=None).date()
        if since == "today":
            cutoff = today
        elif since == "week":
            cutoff = today - timedelta(days=7)
        elif since == "custom":
            cutoff = st.session_state.filter_since_date or today
        else:
            cutoff = None
        if cutoff is not None:
            df = df[df["first_seen"].apply(lambda d: d is not None and d >= cutoff)]
    return df


def render_job_list(df: pd.DataFrame):
    filtered = apply_filters(df)

    # Collapse by cluster_key: aggregate variant cities, keep canonical row's display data.
    # Any sibling matching the active filters keeps the cluster visible; the row shown is
    # always the canonical so clicks route to canonical detail.
    visible_clusters = set(filtered["cluster_key"].unique())
    canonicals = df[df["is_canonical"] & df["cluster_key"].isin(visible_clusters)].copy()

    # Aggregate sibling cities per cluster
    def _agg_cities(cluster_key: str) -> tuple[str, int]:
        cluster_rows = df[df["cluster_key"] == cluster_key]
        cities = []
        seen = set()
        for c in cluster_rows["city"]:
            if c and c not in seen:
                seen.add(c)
                cities.append(c)
        return (", ".join(cities), len(cluster_rows))

    canonicals["_cities"], canonicals["_count"] = zip(
        *canonicals["cluster_key"].map(_agg_cities)
    ) if not canonicals.empty else ([], [])

    # Sort: tier order, then first_seen desc
    canonicals["_tier_ord"] = canonicals["display_tier"].map(lambda t: TIER_ORDER.get(t, 99))
    canonicals = canonicals.sort_values(["_tier_ord", "first_seen"], ascending=[True, False])

    total_jobs = len(df)
    total_clusters = df["cluster_key"].nunique()
    showing = len(canonicals)
    label = f"**{showing}** roles"
    if showing != total_clusters:
        label += f" (filtered from {total_clusters} across {total_jobs} postings)"
    elif total_jobs != total_clusters:
        label += f" ({total_jobs} postings consolidated)"
    st.markdown(label)

    if canonicals.empty:
        st.info("No jobs match the current filters.")
        return

    # "Locations" column: city name plus a (+N) badge when there are siblings
    def _loc_display(row) -> str:
        n = int(row["_count"])
        return f"{row['_cities']}  (+{n-1})" if n > 1 else row["_cities"] or row["city"]

    canonicals["_locations"] = canonicals.apply(_loc_display, axis=1)

    # Build display df
    display = pd.DataFrame({
        "Company": canonicals["company"],
        "Title": canonicals["title"],
        "Locations": canonicals["_locations"],
        "Tier": canonicals["display_tier"],
        "Status": canonicals["app_status"].map(lambda s: APP_STATUS_LABELS.get(s, "—")),
        "Source": canonicals["source"],
        "Seen": canonicals["first_seen"].astype(str),
    })
    # Only surface the posting-status column when inactive postings are in view,
    # so the common active-only list stays uncluttered.
    if st.session_state.include_inactive:
        display.insert(4, "Posting", canonicals["posting_status"].map(POSTING_STATUS_LABELS))
    display.index = canonicals["id"].values

    styled = display.style.map(
        lambda val: TIER_CELL_STYLE.get(val, ""), subset=["Tier"]
    )

    event = st.dataframe(
        styled,
        width="stretch",
        hide_index=True,
        on_select="rerun",
        selection_mode="single-row",
        column_config={
            "Title": st.column_config.TextColumn(width="large"),
            "Locations": st.column_config.TextColumn(width="medium"),
            "Seen": st.column_config.TextColumn(width="small"),
        },
    )

    if event and event.selection and event.selection.rows:
        row_idx = event.selection.rows[0]
        job_id = canonicals.iloc[row_idx]["id"]
        st.session_state.selected_job_id = job_id
        st.rerun()


# ---------------------------------------------------------------------------
# Job detail view
# ---------------------------------------------------------------------------
def render_job_detail(job_id: str):
    job, sc, meta, app, user_id = load_job_detail(job_id)
    if not job:
        st.error("Job not found.")
        return

    # Back button
    if st.button("← Back to list"):
        st.session_state.selected_job_id = None
        st.rerun()

    # Header
    tier_label = sc.tier if sc else "Unscored"
    st.markdown(f"## {job.company} — {job.title}")
    col1, col2, col3 = st.columns([2, 2, 1])
    with col1:
        st.markdown(f"📍 {job.location}")
        st.markdown(f"🔗 [{SOURCE_LABELS.get(job.source, job.source)}]({job.url})")
    with col2:
        st.markdown(f"📅 First seen: {job.first_seen.strftime('%Y-%m-%d') if job.first_seen else '—'}")
        if sc:
            st.markdown(f"🎯 Scored: {sc.scored_at.strftime('%Y-%m-%d') if sc.scored_at else '—'}")
    with col3:
        current_status = meta.app_status if meta else None
        st.markdown(f"**{APP_STATUS_LABELS.get(current_status, '—')}**")

    st.markdown("---")

    # Status buttons
    st.markdown("**Track this job:**")
    status_options = [
        ("applied", "✉️ Applied"), ("interviewing", "💬 Interviewing"),
        ("offer", "🎉 Offer"), ("rejected", "❌ Rejected"),
        ("not_pursuing", "✕ Not Pursuing"), ("clear", "✖ Clear"),
    ]
    status_cols = st.columns(len(status_options))
    current_status = meta.app_status if meta else None
    for i, (val, label) in enumerate(status_options):
        with status_cols[i]:
            if st.button(label, key=f"status_{val}", width="stretch",
                         type="primary" if (current_status == val) else "secondary"):
                set_app_status(job_id, user_id, val)
                st.rerun()

    st.markdown("---")

    # Inline record editor
    with st.expander("✏️ Edit record fields"):
        with st.form(key=f"edit_{job_id}"):
            ed_company = st.text_input("Company", value=job.company or "")
            ed_title   = st.text_input("Title",   value=job.title or "")
            ed_location = st.text_input("Location", value=job.location or "")
            if st.form_submit_button("Save changes"):
                save_job_fields(job_id, ed_company, ed_title, ed_location)
                st.success("Saved.")
                st.rerun()

    # Main tabs
    tabs = ["📋 Overview", "✉️ Cover Letter", "📄 Resume Rec"]
    tab_overview, tab_cl, tab_rr = st.tabs(tabs)

    with tab_overview:
        if sc:
            c1, c2, c3 = st.columns(3)
            c1.metric("Tier", sc.tier)
            c2.metric("Skill Fit", f"{sc.skill_fit}/100")
            c3.metric("Pref Alignment", f"{sc.preference_alignment}/100")
            st.markdown("**Gap Assessment**")
            st.info(sc.gap_assessment)
            st.markdown("**Positioning Notes**")
            st.markdown(sc.positioning_notes)
            with st.expander("Full rationale"):
                st.markdown(sc.rationale)
        else:
            prescreen_status = meta.prescreen_status if meta else None
            if prescreen_status == "rejected":
                reason = (meta.prescreen_reason or "").replace("REJECT - ", "").strip()
                st.warning(f"**Screened out by Haiku:** {reason}")
                if st.button("⚡ Override: Score this job now", type="primary"):
                    with st.spinner("Scoring with Claude Sonnet..."):
                        try:
                            new_sc = score_job_now(job_id, user_id)
                            st.success(f"Scored: **{new_sc.tier}** (skill={new_sc.skill_fit}, pref={new_sc.preference_alignment})")
                            st.rerun()
                        except Exception as e:
                            st.error(f"Scoring failed: {e}")
            elif not (job.description_raw or "").strip():
                st.warning("No job description available — cannot score.")
            else:
                st.info("Not yet scored. Run the pipeline or score individually.")
                if st.button("⚡ Score this job now", type="primary"):
                    with st.spinner("Scoring with Claude Sonnet..."):
                        try:
                            new_sc = score_job_now(job_id, user_id)
                            st.success(f"Scored: **{new_sc.tier}**")
                            st.rerun()
                        except Exception as e:
                            st.error(f"Scoring failed: {e}")

        # JD preview
        if (job.description_raw or "").strip():
            with st.expander("Job description"):
                st.markdown(job.description_raw[:4000])
        else:
            st.caption("No job description on file.")

    with tab_cl:
        if app and app.materials_path:
            cl_text = read_material(app.materials_path, "cover_letter.md")
            if cl_text:
                st.markdown(cl_text)
            else:
                st.warning("Cover letter file not found at expected path.")
        elif sc and sc.tier in ("Strong Fit", "Worth Reviewing"):
            st.info("Materials not yet generated for this job.")
            if st.button("⚡ Generate materials now", key="gen_cl", type="primary"):
                with st.spinner("Generating cover letter and resume recommendation..."):
                    try:
                        out_dir = generate_materials_now(job_id, user_id)
                        st.success(f"Generated → {out_dir}")
                        st.rerun()
                    except Exception as e:
                        st.error(f"Generation failed: {e}")
        else:
            st.info("Score this job first to generate materials.")

    with tab_rr:
        if app and app.materials_path:
            rr_text = read_material(app.materials_path, "resume_recommendation.md")
            jm_text = read_material(app.materials_path, "job.md")
            if rr_text:
                st.markdown(rr_text)
            if jm_text:
                with st.expander("Job summary (job.md)"):
                    st.markdown(jm_text)
            st.markdown("---")
            st.markdown("**📄 Tailored resume**")
            tailored_path = Path(app.materials_path) / "resume_tailored.pptx"
            if tailored_path.exists():
                st.download_button(
                    "⬇️ Download resume_tailored.pptx",
                    tailored_path.read_bytes(),
                    file_name=f"resume_tailored_{job_id[:8]}.pptx",
                    mime="application/vnd.openxmlformats-officedocument.presentationml.presentation",
                    key=f"dl_tailored_{job_id}",
                )
            if st.button("✨ Tailor resume PPTX", key=f"tailor_{job_id}",
                         help="Apply the recommendation above to a copy of the base resume (Drive)"):
                with st.spinner("Downloading base, applying edits, uploading to Drive..."):
                    try:
                        st.session_state[f"tailor_report_{job_id}"] = tailor_resume_now(app.materials_path)
                    except Exception as e:
                        st.error(f"Tailoring failed: {e}")
            report = st.session_state.get(f"tailor_report_{job_id}")
            if report:
                render_tailor_report(report)
        elif sc and sc.tier in ("Strong Fit", "Worth Reviewing"):
            st.info("Materials not yet generated.")
            if st.button("⚡ Generate materials now", key="gen_rr", type="primary"):
                with st.spinner("Generating..."):
                    try:
                        out_dir = generate_materials_now(job_id, user_id)
                        st.success(f"Generated → {out_dir}")
                        st.rerun()
                    except Exception as e:
                        st.error(f"Generation failed: {e}")
        else:
            st.info("Score this job first to generate materials.")


# ---------------------------------------------------------------------------
# Add Job view (paste URL → fetch → review/edit → save)
# ---------------------------------------------------------------------------
def _save_manual_job(company: str, title: str, location: str, description: str, url: str) -> str:
    """Insert/update a Job row from manual entry. Returns the job_id."""
    init_db()
    company = (company or "Unknown").strip()
    title = (title or "Unknown Role").strip()
    location = (location or "").strip()
    job_db_id = compute_job_id(company, title, location)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    job = Job(
        id=job_db_id,
        source="manual",
        company=company,
        title=title,
        location=location,
        url=url.strip(),
        description_raw=description or "",
        first_seen=now,
        last_seen=now,
        status="active",
    )
    with get_session() as s:
        upsert_job(s, job)
        s.commit()
    st.cache_data.clear()
    return job_db_id


def render_add_job(user_id: int | None):
    st.markdown("## ➕ Add a job by URL")
    st.caption("Paste a job posting URL. The system tries to extract title, company, location, and the JD — review and edit before saving.")

    url = st.text_input(
        "Job URL",
        value=st.session_state.add_url_input,
        placeholder="https://…",
        key="add_url_widget",
    )

    col_fetch, col_clear = st.columns([1, 4])
    with col_fetch:
        do_fetch = st.button("Fetch", type="primary", disabled=not url.strip())
    with col_clear:
        if st.session_state.add_fetched is not None:
            if st.button("Clear / start over"):
                st.session_state.add_fetched = None
                st.session_state.add_url_input = ""
                st.rerun()

    if do_fetch:
        st.session_state.add_url_input = url
        with st.spinner("Fetching and extracting…"):
            try:
                st.session_state.add_fetched = fetch_from_url(url)
            except Exception as e:
                st.session_state.add_fetched = {
                    "title": "", "company": "", "location": "", "description": "", "url": url,
                }
                st.warning(f"Fetch failed ({e}). You can still fill in the form manually below.")

    # If nothing fetched yet, also offer a "Manual entry" shortcut that opens a blank form.
    if st.session_state.add_fetched is None:
        if url.strip() and st.button("Skip fetch — enter everything manually"):
            st.session_state.add_url_input = url
            st.session_state.add_fetched = {
                "title": "", "company": "", "location": "", "description": "", "url": url,
            }
            st.rerun()
        st.info("Paste a URL and click **Fetch**. If extraction fails, you'll see a blank form to fill in manually.")
        return

    fetched = st.session_state.add_fetched
    any_extracted = any(fetched.get(k) for k in ("title", "company", "description"))
    if not any_extracted:
        st.warning("Couldn't extract anything from the URL. Fill in the fields below manually.")
    else:
        st.success("Extracted fields below. Review and edit before saving.")

    # Editable form
    with st.form(key="add_job_form"):
        ed_company  = st.text_input("Company",  value=fetched.get("company", ""))
        ed_title    = st.text_input("Title",    value=fetched.get("title", ""))
        ed_location = st.text_input("Location", value=fetched.get("location", ""))
        ed_url      = st.text_input("URL",      value=fetched.get("url", url))
        ed_desc     = st.text_area(
            "Job description",
            value=fetched.get("description", ""),
            height=300,
            help="Plain text. The scoring prompt uses this directly.",
        )
        score_now = st.checkbox(
            "Score this job immediately after saving",
            value=True,
            help="Runs the standard Sonnet scoring on save. Marks as manual_override so prescreen is skipped.",
        )
        submitted = st.form_submit_button("Save", type="primary")

    if submitted:
        if not (ed_title.strip() or ed_company.strip()):
            st.error("At least one of Company or Title is required.")
            return
        job_db_id = _save_manual_job(
            company=ed_company, title=ed_title, location=ed_location,
            description=ed_desc, url=ed_url,
        )
        st.success(f"Saved → **{ed_company} — {ed_title}**")

        if score_now and user_id is not None:
            if len((ed_desc or "").strip()) < 100:
                st.warning("JD is too short to score (<100 chars). Saved without scoring; add description and use 'Score this job now' on the detail page.")
            else:
                with st.spinner("Scoring with Claude Sonnet…"):
                    try:
                        sc = score_one(job_db_id, user_id)
                        st.success(f"Scored: **{sc.tier}** (skill={sc.skill_fit}, pref={sc.preference_alignment})")
                    except Exception as e:
                        st.error(f"Scoring failed: {e}. Job is saved — you can score from the detail page.")

        # Clear add-job state and route to the new job's detail page
        st.session_state.add_fetched = None
        st.session_state.add_url_input = ""
        st.session_state.selected_job_id = job_db_id
        st.session_state.page = "jobs"
        st.rerun()


# ---------------------------------------------------------------------------
# Stats view
# ---------------------------------------------------------------------------
# Chart palettes (aligned with the tier/status colours used elsewhere in the UI)
TIER_CHART_COLORS = {
    "Strong Fit": "#2e7d32", "Worth Reviewing": "#f2c94c",
    "Long Shot": "#eb8c34", "Skip": "#777777",
}
STATUS_CHART_COLORS = {
    "applied": "#4c78a8", "interviewing": "#9467bd", "offer": "#2ca02c",
    "rejected": "#d62728", "not_pursuing": "#8c8c8c",
}
STAGE_CHART_COLORS = {"Prescreen": "#f2c94c", "Score": "#eb8c34", "Generate": "#c2185b"}


def _cumulative(frame: pd.DataFrame, label: str) -> pd.DataFrame:
    """Daily cumulative count of events in `frame` (expects a 'date' column)."""
    if frame is None or frame.empty:
        return pd.DataFrame(columns=["date", "cum", "stage"])
    daily = frame.groupby(frame["date"].dt.normalize()).size().sort_index().cumsum()
    out = daily.reset_index()
    out.columns = ["date", "cum"]
    out["stage"] = label
    return out


def _render_funnel(funnel):
    st.markdown("#### The funnel")
    fdf = pd.DataFrame(funnel, columns=["stage", "count"])
    top = fdf["count"].iloc[0] or 1
    fdf["pct"] = fdf["count"] / top * 100
    fdf["label"] = fdf.apply(lambda r: f"{int(r['count']):,}  ·  {r['pct']:.1f}%", axis=1)
    order = fdf["stage"].tolist()
    base = alt.Chart(fdf).encode(
        y=alt.Y("stage:N", sort=order, title=None),
        x=alt.X("count:Q", title="Jobs (log scale)", scale=alt.Scale(type="symlog")),
    )
    bars = base.mark_bar(color="#4c78a8", cornerRadiusEnd=3)
    text = base.mark_text(align="left", dx=4, color="#ddd").encode(text="label:N")
    st.altair_chart(bars + text, use_container_width=True)
    st.caption(
        "Percentages are share of all discovered postings. Bar length is log-scaled so "
        "the small end of the funnel stays visible."
    )


def _render_pipeline_over_time(ts):
    st.markdown("#### Pipeline over time")
    lines = pd.concat([
        _cumulative(ts["discovery"], "Discovered"),
        _cumulative(ts["scoring"], "Scored"),
        _cumulative(ts["generation"], "Materials made"),
    ], ignore_index=True)
    if lines.empty:
        st.info("Not enough dated activity yet.")
        return
    color = alt.Color(
        "stage:N",
        scale=alt.Scale(
            domain=["Discovered", "Scored", "Materials made"],
            range=["#4c78a8", "#eb8c34", "#2ca02c"],
        ),
        title="Cumulative",
    )
    chart = alt.Chart(lines).mark_line(interpolate="step-after").encode(
        x=alt.X("date:T", title=None),
        y=alt.Y("cum:Q", title="Cumulative jobs"),
        color=color,
        tooltip=["stage:N", alt.Tooltip("date:T"), alt.Tooltip("cum:Q", title="total")],
    ).properties(height=280)
    st.altair_chart(chart, use_container_width=True)
    st.caption("Running totals across the whole search — the funnel filling up week by week.")


def _render_intake_and_quality(ts):
    c1, c2 = st.columns(2)
    with c1:
        st.markdown("#### Intake by week")
        d = ts["discovery"]
        if d.empty:
            st.info("No discovery data.")
        else:
            d = d.assign(week=d["date"].dt.to_period("W").apply(lambda p: p.start_time))
            agg = d.groupby(["week", "source"]).size().reset_index(name="count")
            chart = alt.Chart(agg).mark_bar().encode(
                x=alt.X("week:T", title=None),
                y=alt.Y("count:Q", title="Jobs discovered"),
                color=alt.Color("source:N", title="Source"),
                tooltip=["source:N", alt.Tooltip("week:T"), "count:Q"],
            ).properties(height=260)
            st.altair_chart(chart, use_container_width=True)
    with c2:
        st.markdown("#### Score quality by week")
        s = ts["scoring"]
        if s.empty:
            st.info("No scoring data.")
        else:
            s = s.assign(week=s["date"].dt.to_period("W").apply(lambda p: p.start_time))
            agg = s.groupby(["week", "tier"]).size().reset_index(name="count")
            chart = alt.Chart(agg).mark_bar().encode(
                x=alt.X("week:T", title=None),
                y=alt.Y("count:Q", title="Jobs scored"),
                color=alt.Color(
                    "tier:N",
                    scale=alt.Scale(domain=list(TIER_CHART_COLORS),
                                    range=list(TIER_CHART_COLORS.values())),
                    title="Tier",
                ),
                order=alt.Order("tier:N"),
                tooltip=["tier:N", alt.Tooltip("week:T"), "count:Q"],
            ).properties(height=260)
            st.altair_chart(chart, use_container_width=True)


def _render_application_journey(ts):
    st.markdown("#### Application journey")
    j = ts["journey"]
    if j.empty:
        st.info("No applications tracked yet — mark jobs as applied/rejected in the job detail view.")
        return

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("✉️ Applied", int((j["app_status"] == "applied").sum()))
    m2.metric("❌ Rejected", int((j["app_status"] == "rejected").sum()))
    m3.metric("✕ Not pursuing", int((j["app_status"] == "not_pursuing").sum()))
    applied = j[(j["app_status"] == "applied") & j["first_seen"].notna()].copy()
    if not applied.empty:
        days = (applied["date"] - pd.to_datetime(applied["first_seen"])).dt.days
        m4.metric("Median days: found → applied", f"{int(days.median())}d")

    statuses = [s for s in STATUS_CHART_COLORS if s in set(j["app_status"])]
    points = alt.Chart(j).mark_circle(size=140, opacity=0.85).encode(
        x=alt.X("date:T", title=None),
        y=alt.Y("app_status:N", sort=statuses, title=None,
                axis=alt.Axis(labelExpr="replace(datum.label, '_', ' ')")),
        color=alt.Color(
            "app_status:N",
            scale=alt.Scale(domain=statuses,
                            range=[STATUS_CHART_COLORS[s] for s in statuses]),
            legend=None,
        ),
        tooltip=[
            alt.Tooltip("date:T", title="when"),
            alt.Tooltip("company:N"), alt.Tooltip("title:N"),
            alt.Tooltip("app_status:N", title="status"), alt.Tooltip("tier:N"),
        ],
    ).properties(height=180)
    st.altair_chart(points, use_container_width=True)
    st.caption(
        "Each dot is a job at its **current** tracked status and the date it last changed — "
        "the pipeline stores only the latest state, not every transition, so a job applied "
        "then rejected appears once, at the rejection."
    )


def _render_api_usage(ts):
    st.markdown("#### Estimated API usage")
    counts = ts["cost_counts"]

    stage_labels = {
        "prescreen": "Prescreen", "score": "Score",
        "cover_letter_strong": "Cover letter (Strong Fit)",
        "cover_letter_other": "Cover letter (other)",
        "resume_rec": "Resume rec",
    }
    rows = []
    for stage, calls in counts.items():
        if not calls:
            continue
        spec = STAGE_COST_MODEL[stage]
        rows.append({
            "Stage": stage_labels[stage],
            "Model": MODEL_PRICING[spec["model"]]["label"],
            "Batch": "✓" if spec["batch"] else "",
            "Calls": calls,
            "Est. input tok": calls * spec["in"],
            "Est. output tok": calls * spec["out"],
            "Est. cost (USD)": calls * _stage_call_cost(stage),
        })
    table = pd.DataFrame(rows)
    total = float(table["Est. cost (USD)"].sum()) if not table.empty else 0.0

    a, b, c = st.columns([1, 1, 2])
    a.metric("Est. total spend", f"${total:,.2f}")
    total_calls = int(table["Calls"].sum()) if not table.empty else 0
    b.metric("Total API calls", f"{total_calls:,}")
    c.metric("Est. cost / application", f"${total / max(int((ts['journey']['app_status'] == 'applied').sum()), 1):,.2f}"
             if not ts["journey"].empty else "—")

    st.dataframe(
        table.style.format({
            "Calls": "{:,}", "Est. input tok": "{:,}",
            "Est. output tok": "{:,}", "Est. cost (USD)": "${:,.2f}",
        }),
        hide_index=True, width="stretch",
    )

    # Estimated weekly spend, split by stage group (using each stage's real dates;
    # prescreen has no per-call timestamp so it's placed at discovery time).
    spend_frames = []
    disc = ts["discovery"]
    if not disc.empty and counts.get("prescreen"):
        # Scale so the discovery-timed proxy sums to the true screen count.
        per = _stage_call_cost("prescreen") * counts["prescreen"] / len(disc)
        spend_frames.append(disc.assign(group="Prescreen", cost=per)[["date", "group", "cost"]])
    sc = ts["scoring"]
    if not sc.empty:
        spend_frames.append(sc.assign(group="Score", cost=_stage_call_cost("score"))[["date", "group", "cost"]])
    gen = ts["generation"]
    if not gen.empty:
        gcost = gen["tier"].map(
            lambda t: _stage_call_cost("resume_rec") + _stage_call_cost(
                "cover_letter_strong" if t == "Strong Fit" else "cover_letter_other")
        )
        spend_frames.append(gen.assign(group="Generate", cost=gcost)[["date", "group", "cost"]])

    if spend_frames:
        spend = pd.concat(spend_frames, ignore_index=True)
        spend["week"] = spend["date"].dt.to_period("W").apply(lambda p: p.start_time)
        agg = spend.groupby(["week", "group"])["cost"].sum().reset_index()
        chart = alt.Chart(agg).mark_bar().encode(
            x=alt.X("week:T", title=None),
            y=alt.Y("cost:Q", title="Est. USD"),
            color=alt.Color("group:N",
                            scale=alt.Scale(domain=list(STAGE_CHART_COLORS),
                                            range=list(STAGE_CHART_COLORS.values())),
                            title="Stage"),
            tooltip=["group:N", alt.Tooltip("week:T"), alt.Tooltip("cost:Q", format="$.2f")],
        ).properties(height=240)
        st.altair_chart(chart, use_container_width=True)

    with st.expander("How this estimate is built"):
        st.markdown(
            "This is an **estimate, not billed usage** — the pipeline doesn't record token "
            "counts. It multiplies real per-stage call volumes (from the DB) by the model each "
            "stage uses and published list pricing, applying the 50% Batch API discount to "
            "prescreen and scoring. Per-call token sizes are derived from the truncation limits "
            "in `settings.py`. Prescreen has no per-call timestamp, so on the weekly chart its "
            "cost is placed at each job's discovery date."
        )
        assume = pd.DataFrame([
            {"Stage": stage_labels[k], "Model": MODEL_PRICING[v["model"]]["label"],
             "In tok/call": v["in"], "Out tok/call": v["out"], "Batch": "✓" if v["batch"] else ""}
            for k, v in STAGE_COST_MODEL.items()
        ])
        st.dataframe(assume, hide_index=True, width="stretch")
        price = pd.DataFrame([
            {"Model": v["label"], "$/Mtok in": v["in"], "$/Mtok out": v["out"]}
            for v in MODEL_PRICING.values()
        ])
        st.dataframe(price, hide_index=True, width="stretch")


def render_stats(df: pd.DataFrame):
    st.markdown("## 📊 Pipeline Stats")

    if df is None or df.empty:
        st.info("No jobs in the database yet.")
        return

    ts = load_stats_timeseries()

    col1, col2, col3, col4, col5 = st.columns(5)
    col1.metric("Total Jobs", len(df))
    scored = df[df["scored"]]
    col2.metric("Scored", len(scored))
    strong = df[df["tier"] == "Strong Fit"]
    col3.metric("Strong Fit", len(strong))
    applied = df[df["app_status"].isin(["applied", "interviewing", "offer"])]
    col4.metric("In Progress", len(applied))
    today = datetime.now(timezone.utc).replace(tzinfo=None).date()
    new_today = df[df["first_seen"].apply(lambda d: d is not None and d >= today)]
    col5.metric("New Today", len(new_today))

    st.markdown("---")
    st.markdown("### 🧭 Your search journey")
    st.caption("Full history — every posting ever discovered, independent of the sidebar filter.")

    _render_funnel(ts["funnel"])
    _render_pipeline_over_time(ts)
    _render_intake_and_quality(ts)
    _render_application_journey(ts)
    _render_api_usage(ts)

    st.markdown("---")
    st.markdown("### Breakdowns")
    st.caption("These tables reflect the current job list (respects the sidebar filter).")
    c1, c2 = st.columns(2)

    with c1:
        st.markdown("**By Tier**")
        tier_counts = df["tier"].value_counts().reset_index()
        tier_counts.columns = ["Tier", "Count"]
        st.dataframe(tier_counts, hide_index=True, width="stretch")

    with c2:
        st.markdown("**By Source**")
        src_counts = df["source"].value_counts().reset_index()
        src_counts.columns = ["Source", "Count"]
        st.dataframe(src_counts, hide_index=True, width="stretch")

    st.markdown("**By Company (top 20)**")
    co_counts = df["company"].value_counts().head(20).reset_index()
    co_counts.columns = ["Company", "Jobs"]
    st.dataframe(co_counts, hide_index=True, width="stretch")

    screened_out_count = len(df[df["display_tier"] == "Screened Out"])
    if screened_out_count:
        st.metric("Screened Out (Haiku)", screened_out_count)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    df, user_id = load_jobs_df(st.session_state.include_inactive)
    render_sidebar(df)

    page = st.session_state.page
    job_id = st.session_state.selected_job_id

    if page == "jobs":
        if job_id:
            render_job_detail(job_id)
        else:
            st.markdown("## 📋 Jobs")
            render_job_list(df)
    elif page == "add":
        render_add_job(user_id)
    elif page == "stats":
        render_stats(df)


if __name__ == "__main__":
    main()
