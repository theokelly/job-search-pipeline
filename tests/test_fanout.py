"""Tests for fan-out discovery: query building, title gate, URL identity,
Brave provider parsing, and the fetch_jobs orchestration (all HTTP faked)."""
import httpx
import pytest

from job_agent import settings
from job_agent.config import ProfileError, validate_profile
from job_agent.db import Job
from job_agent.discovery import fanout, search_provider


PROFILE = {
    "fanout": {
        "provider": "brave",
        "domains": ["jobs.ashbyhq.com", "jobs.lever.co"],
        "phrase_domains": ["jobs.ashbyhq.com"],
        "phrase_count": 2,
        "title_phrases": ["Head of AI", "AI Enablement Lead", "Director of AI"],
        "results_per_query": 10,
        "max_candidate_urls": 200,
        "max_fetches": 60,
    }
}

ASHBY_URL = "https://jobs.ashbyhq.com/n8n/fea09cae-58f1-4f3e-b4a0-daba6e7e5002"


@pytest.fixture(autouse=True)
def _no_dead_url_db(monkeypatch):
    """Keep fetch_jobs tests hermetic: the dead-URL gate hits the real
    jobs DB by default. Point get_session at a failing stub — the gate
    helpers are best-effort and degrade to no-ops — unless a test
    installs its own session factory or stubs the helpers directly."""
    from contextlib import contextmanager

    @contextmanager
    def _boom():
        raise RuntimeError("no DB in tests")
        yield  # pragma: no cover

    monkeypatch.setattr(fanout, "get_session", _boom)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def test_fanout_config_defaults():
    cfg = settings.fanout_config({})
    assert cfg["provider"] == "brave"
    assert cfg["enabled_by_default"] is False
    assert "jobs.ashbyhq.com" in cfg["domains"]
    assert "AI Enablement Lead" in cfg["title_phrases"]


def test_fanout_profile_validation():
    assert validate_profile(PROFILE) is PROFILE
    with pytest.raises(ProfileError):
        validate_profile({"fanout": {"max_fetches": "60"}})


# ---------------------------------------------------------------------------
# Queries and the title gate
# ---------------------------------------------------------------------------

def test_build_queries():
    cfg = settings.fanout_config(PROFILE)
    queries = fanout.build_queries(cfg)
    # Tier 1: one combined query per domain; tier 2: phrase queries for the
    # phrase_domains x first phrase_count phrases.
    assert 'site:jobs.ashbyhq.com ("Head of AI" OR "AI Enablement Lead" OR "Director of AI")' in queries
    assert 'site:jobs.lever.co ("Head of AI" OR "AI Enablement Lead" OR "Director of AI")' in queries
    assert 'site:jobs.ashbyhq.com "Head of AI"' in queries
    assert 'site:jobs.ashbyhq.com "AI Enablement Lead"' in queries
    assert len(queries) == 4
    for q in queries:  # Brave rejects >50-word queries
        assert len(q.split()) <= 50


def test_title_matches():
    phrases = PROFILE["fanout"]["title_phrases"]
    assert fanout.title_matches("Internal AI Enablement Lead", phrases)
    assert fanout.title_matches("Internal AI Enablement Lead @ n8n", phrases)
    assert fanout.title_matches("Head of AI, EMEA", phrases)
    assert not fanout.title_matches("Sales Enablement Manager", phrases)
    assert not fanout.title_matches("", phrases)


# ---------------------------------------------------------------------------
# URL identity
# ---------------------------------------------------------------------------

def test_parse_ats_url():
    assert fanout.parse_ats_url(ASHBY_URL) == (
        "ashby", "n8n", "fea09cae-58f1-4f3e-b4a0-daba6e7e5002")
    assert fanout.parse_ats_url(
        "https://job-boards.greenhouse.io/catawiki/jobs/8288546"
    ) == ("greenhouse", "catawiki", "8288546")
    assert fanout.parse_ats_url(
        "https://jobs.lever.co/acme/123e4567-e89b-12d3-a456-426614174000"
    ) == ("lever", "acme", "123e4567-e89b-12d3-a456-426614174000")
    assert fanout.parse_ats_url("https://example.com/careers/123") is None


def test_normalize_url_ashby_application_and_query():
    applied = ASHBY_URL + "/application?workplaceType=Remote"
    assert fanout.normalize_url(applied) == ASHBY_URL
    assert fanout.normalize_url(ASHBY_URL + "?utm_source=google") == ASHBY_URL


def test_normalize_url_greenhouse_host_collapses():
    a = fanout.normalize_url("https://boards.greenhouse.io/acme/jobs/42?gh_src=x")
    b = fanout.normalize_url("https://job-boards.greenhouse.io/acme/jobs/42")
    assert a == b == "https://job-boards.greenhouse.io/acme/jobs/42"


def test_normalize_url_generic_strips_tracking():
    assert fanout.normalize_url(
        "https://acme.recruitee.com/o/head-of-ai?utm_medium=email#apply"
    ) == "https://acme.recruitee.com/o/head-of-ai"


# ---------------------------------------------------------------------------
# Brave provider
# ---------------------------------------------------------------------------

def test_brave_search_parses_results(monkeypatch):
    monkeypatch.setenv("BRAVE_API_KEY", "test-key")
    calls = {}

    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"web": {"results": [
                {"title": "Internal AI Enablement Lead", "url": ASHBY_URL,
                 "description": "Build the enablement function...",
                 "page_age": "2026-11-05T08:30:00"},
                {"title": "Other", "url": "https://example.com/x", "description": ""},
            ]}}

    def fake_get(url, headers=None, params=None, timeout=None):
        calls["url"] = url
        calls["token"] = headers["X-Subscription-Token"]
        calls["params"] = params
        return FakeResp()

    monkeypatch.setattr(search_provider.httpx, "get", fake_get)
    results = search_provider.search('site:jobs.ashbyhq.com "AI Enablement Lead"', num=10)
    assert calls["url"] == "https://api.search.brave.com/res/v1/web/search"
    assert calls["token"] == "test-key"
    assert calls["params"]["count"] == 10
    assert results[0] == {
        "title": "Internal AI Enablement Lead", "url": ASHBY_URL,
        "snippet": "Build the enablement function...",
        "page_age": "2026-11-05T08:30:00",
    }
    assert results[1]["page_age"] is None


def test_brave_search_missing_key(monkeypatch):
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    monkeypatch.delenv("BRAVE_SEARCH_API_KEY", raising=False)
    with pytest.raises(EnvironmentError):
        search_provider.search("anything")


# ---------------------------------------------------------------------------
# fetch_jobs orchestration
# ---------------------------------------------------------------------------

def _fake_job(url, title="Internal AI Enablement Lead"):
    return Job(
        id="x" * 16, source="ashby_api", company="N8n", title=title,
        location="Berlin Office (Remote)", url=url,
        description_raw="A real JD body.", status="active",
    )


def test_fetch_jobs_gates_dedupes_and_marks_source(monkeypatch):
    seen_queries = []

    def fake_search(query, num=10):
        seen_queries.append(query)
        return [
            {"title": "Internal AI Enablement Lead", "url": ASHBY_URL, "snippet": ""},
            # Same posting via the application URL: a within-run duplicate.
            {"title": "Internal AI Enablement Lead",
             "url": ASHBY_URL + "/application?workplaceType=Remote", "snippet": ""},
            # Title gate drops this before any fetch happens.
            {"title": "Sales Enablement Manager",
             "url": "https://jobs.ashbyhq.com/n8n/b45f4511-fae8-4ea1-ae88-f17d26f13acb",
             "snippet": ""},
        ]

    fetched_urls = []

    def fake_fetch(url):
        fetched_urls.append(url)
        return _fake_job(url)

    monkeypatch.setattr(fanout.search_provider, "search", fake_search)
    monkeypatch.setattr(fanout, "fetch_job_from_url", fake_fetch)
    jobs, stats = fanout.fetch_jobs(PROFILE)
    assert len(jobs) == 1
    assert jobs[0].source == "search_fanout"
    assert jobs[0].url == ASHBY_URL  # canonical URL stored
    assert fetched_urls == [ASHBY_URL]  # duplicate + gated result never fetched
    assert stats["candidates"] == 2
    assert stats["gated_out"] == 1
    assert stats["extract_failed"] == 0


def test_fetch_jobs_counts_404_as_gone_not_error(monkeypatch):
    # A posting that was live at index time and has since closed makes the
    # ATS API return 404. That is churn, not an error: it lands in `gone`
    # and in neither `errors` nor `extract_failed`.
    def fake_search(query, num=10):
        return [{"title": "Head of AI", "url": ASHBY_URL, "snippet": ""}]

    def fake_fetch(url):
        raise httpx.HTTPStatusError(
            "404 Not Found",
            request=httpx.Request("GET", url),
            response=httpx.Response(404),
        )

    monkeypatch.setattr(fanout.search_provider, "search", fake_search)
    monkeypatch.setattr(fanout, "fetch_job_from_url", fake_fetch)
    jobs, stats = fanout.fetch_jobs(PROFILE)
    assert jobs == []
    assert stats["gone"] == 1
    assert stats["errors"] == []
    assert stats["extract_failed"] == 0


def test_fetch_jobs_unsupported_provider():
    with pytest.raises(EnvironmentError):
        fanout.fetch_jobs({"fanout": {"provider": "serper"}})


# ---------------------------------------------------------------------------
# Fetch gates: known/dead skips, gone recording, freshness order
# ---------------------------------------------------------------------------

LEVER_URL = "https://jobs.lever.co/acme/123e4567-e89b-12d3-a456-426614174000"


def test_fetch_jobs_skips_known_urls(monkeypatch):
    def fake_search(query, num=10):
        return [{"title": "Internal AI Enablement Lead", "url": ASHBY_URL, "snippet": ""}]

    def fake_fetch(url):
        raise AssertionError("known URL must not be fetched")

    monkeypatch.setattr(fanout.search_provider, "search", fake_search)
    monkeypatch.setattr(fanout, "fetch_job_from_url", fake_fetch)
    jobs, stats = fanout.fetch_jobs(PROFILE, known_urls={ASHBY_URL})
    assert jobs == []
    assert stats["known_skipped"] == 1
    assert stats["fetched"] == 0


def test_fetch_jobs_skips_recorded_dead_urls(monkeypatch):
    monkeypatch.setattr(fanout, "_load_dead_urls", lambda ttl_days: {ASHBY_URL})

    def fake_search(query, num=10):
        return [{"title": "Internal AI Enablement Lead", "url": ASHBY_URL, "snippet": ""}]

    def fake_fetch(url):
        raise AssertionError("dead URL must not be fetched")

    monkeypatch.setattr(fanout.search_provider, "search", fake_search)
    monkeypatch.setattr(fanout, "fetch_job_from_url", fake_fetch)
    jobs, stats = fanout.fetch_jobs(PROFILE)
    assert jobs == []
    assert stats["dead_skipped"] == 1
    assert stats["fetched"] == 0


def test_fetch_jobs_records_gone_urls(monkeypatch):
    recorded = []
    monkeypatch.setattr(fanout, "_record_dead_urls", recorded.extend)

    def fake_search(query, num=10):
        return [{"title": "Head of AI", "url": ASHBY_URL, "snippet": ""}]

    def fake_fetch(url):
        raise httpx.HTTPStatusError(
            "404 Not Found",
            request=httpx.Request("GET", url),
            response=httpx.Response(404),
        )

    monkeypatch.setattr(fanout.search_provider, "search", fake_search)
    monkeypatch.setattr(fanout, "fetch_job_from_url", fake_fetch)
    jobs, stats = fanout.fetch_jobs(PROFILE)
    assert stats["gone"] == 1
    assert recorded == [ASHBY_URL]


def test_fetch_jobs_ashby_board_miss_counts_as_gone(monkeypatch):
    # The Ashby path lists the board's live postings; a posting absent from
    # that list has closed. It must count as gone, not an extraction failure.
    from job_agent.discovery import ashby

    monkeypatch.setattr(ashby, "fetch_jobs", lambda company, board: [])

    def fake_search(query, num=10):
        return [{"title": "Internal AI Enablement Lead", "url": ASHBY_URL, "snippet": ""}]

    monkeypatch.setattr(fanout.search_provider, "search", fake_search)
    jobs, stats = fanout.fetch_jobs(PROFILE)
    assert jobs == []
    assert stats["gone"] == 1
    assert stats["extract_failed"] == 0
    assert stats["errors"] == []


def test_fetch_jobs_fetches_freshest_first(monkeypatch):
    stale = {"title": "Internal AI Enablement Lead", "url": ASHBY_URL,
             "snippet": "", "page_age": "2026-01-15T00:00:00"}
    fresh = {"title": "Head of AI", "url": LEVER_URL,
             "snippet": "", "page_age": "2026-11-05T00:00:00"}

    def fake_search(query, num=10):
        return [stale, fresh]  # stale listed first: order must come from page_age

    fetched_urls = []

    def fake_fetch(url):
        fetched_urls.append(url)
        return _fake_job(url)

    monkeypatch.setattr(fanout.search_provider, "search", fake_search)
    monkeypatch.setattr(fanout, "fetch_job_from_url", fake_fetch)
    profile = {"fanout": {**PROFILE["fanout"], "max_fetches": 1}}
    jobs, stats = fanout.fetch_jobs(profile)
    assert fetched_urls == [LEVER_URL]
    assert stats["fetched"] == 1


def test_dead_url_roundtrip(tmp_path, monkeypatch):
    # Exercise the real load/record helpers against a throwaway DB.
    from contextlib import contextmanager

    from sqlmodel import Session, SQLModel, create_engine

    engine = create_engine(f"sqlite:///{tmp_path}/test.sqlite")
    SQLModel.metadata.create_all(engine)

    @contextmanager
    def fake_session():
        with Session(engine) as s:
            yield s

    monkeypatch.setattr(fanout, "get_session", fake_session)
    fanout._record_dead_urls([ASHBY_URL])
    assert fanout._load_dead_urls(30) == {ASHBY_URL}
    assert fanout._load_dead_urls(-1) == set()  # negative TTL: everything expired
