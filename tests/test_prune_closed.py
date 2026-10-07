"""Unit tests for LinkedIn closed-posting detection (no network)."""
from bs4 import BeautifulSoup

from job_agent.discovery import linkedin


def _soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "html.parser")


# --- is_linkedin_url -------------------------------------------------------

def test_is_linkedin_url():
    assert linkedin.is_linkedin_url("https://www.linkedin.com/jobs/view/4442685002/")
    assert linkedin.is_linkedin_url("https://nl.linkedin.com/jobs/view/x-123")
    assert not linkedin.is_linkedin_url("https://boards.greenhouse.io/acme/jobs/1")
    assert not linkedin.is_linkedin_url("")
    assert not linkedin.is_linkedin_url(None)


# --- _extract_job_id -------------------------------------------------------

def test_extract_job_id_view_url():
    assert linkedin._extract_job_id("https://www.linkedin.com/jobs/view/4442685002/") == "4442685002"


def test_extract_job_id_slug_url():
    url = "https://nl.linkedin.com/jobs/view/senior-business-transformation-lead-at-super-4442685002"
    assert linkedin._extract_job_id(url) == "4442685002"


def test_extract_job_id_query_param():
    url = "https://www.linkedin.com/jobs/search/?currentJobId=4442685002&keywords=x"
    assert linkedin._extract_job_id(url) == "4442685002"


def test_extract_job_id_missing():
    assert linkedin._extract_job_id("https://www.linkedin.com/jobs/") is None


# --- _page_shows_closed ----------------------------------------------------

def test_closed_detected_via_phrase_ignores_obfuscated_classes():
    # Mirrors the real warning element the user reported — obfuscated wrapper
    # classes, phrase inside a <p>. Detection must not rely on the class names.
    html = """
    <div class="_37d1e1ab _5213c67d f76445d2" aria-live="assertive">
      <svg aria-label="Error"></svg>
      <p class="_8afe7807 _474356d6">No longer accepting applications</p>
    </div>
    """
    assert linkedin._page_shows_closed(_soup(html)) is True


def test_closed_detected_via_figcaption():
    html = '<figcaption class="closed-job">No longer accepting applications</figcaption>'
    assert linkedin._page_shows_closed(_soup(html)) is True


def test_closed_detected_case_insensitive():
    html = "<div>NO LONGER ACCEPTING APPLICATIONS</div>"
    assert linkedin._page_shows_closed(_soup(html)) is True


def test_open_posting_not_flagged():
    html = '<div><button class="apply-button">Apply</button><p>165 applicants</p></div>'
    assert linkedin._page_shows_closed(_soup(html)) is False


def test_none_soup_not_flagged():
    assert linkedin._page_shows_closed(None) is False


# --- is_posting_closed (fetch mocked) --------------------------------------

def test_is_posting_closed_returns_none_for_non_linkedin():
    assert linkedin.is_posting_closed("https://greenhouse.io/x/1") is None


def test_is_posting_closed_returns_none_on_fetch_failure(monkeypatch):
    # Both endpoints fail to fetch (status None) -> unknown, do not prune.
    monkeypatch.setattr(linkedin, "_fetch", lambda url, timeout=6: (None, None))
    assert linkedin.is_posting_closed("https://www.linkedin.com/jobs/view/123/") is None


def test_is_posting_closed_returns_none_on_rate_limit(monkeypatch):
    # 429 on both endpoints is not a closed signal -> unknown, do not prune.
    monkeypatch.setattr(linkedin, "_fetch", lambda url, timeout=6: (429, ""))
    assert linkedin.is_posting_closed("https://www.linkedin.com/jobs/view/123/") is None


def test_is_posting_closed_true(monkeypatch):
    closed = '<figcaption class="closed-job">No longer accepting applications</figcaption>'
    monkeypatch.setattr(linkedin, "_fetch", lambda url, timeout=6: (200, closed))
    assert linkedin.is_posting_closed("https://www.linkedin.com/jobs/view/123/") is True


def test_is_posting_closed_false(monkeypatch):
    open_page = '<button class="apply-button">Apply</button>'
    monkeypatch.setattr(linkedin, "_fetch", lambda url, timeout=6: (200, open_page))
    assert linkedin.is_posting_closed("https://www.linkedin.com/jobs/view/123/") is False


def test_is_posting_closed_falls_back_to_view_page(monkeypatch):
    # Fragment endpoint 404s; the full view page is the authority and shows open.
    calls = []

    def fake_fetch(url, timeout=6):
        calls.append(url)
        if "jobs-guest" in url:
            return (404, "")
        return (200, '<button class="apply-button">Apply</button>')

    monkeypatch.setattr(linkedin, "_fetch", fake_fetch)
    assert linkedin.is_posting_closed("https://www.linkedin.com/jobs/view/123/") is False
    assert len(calls) == 2  # fragment, then view-page fallback
