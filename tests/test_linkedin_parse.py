"""Characterization tests for discovery/linkedin.py parsers (Phase C).

URL/id extraction, closed-posting detection, and search-card parsing —
all offline. is_posting_closed is tested through its _fetch seam.
"""
from bs4 import BeautifulSoup

from job_agent.discovery import linkedin


def _soup(html):
    return BeautifulSoup(html, "html.parser")


# ---------------------------------------------------------------------------
# URL + id extraction
# ---------------------------------------------------------------------------

def test_is_linkedin_url():
    assert linkedin.is_linkedin_url("https://www.linkedin.com/jobs/view/123")
    assert not linkedin.is_linkedin_url("https://example.com/jobs/123")
    assert not linkedin.is_linkedin_url("")
    assert not linkedin.is_linkedin_url(None)


def test_extract_job_id_view_url():
    assert linkedin._extract_job_id("https://www.linkedin.com/jobs/view/4321098765") == "4321098765"
    assert linkedin._extract_job_id("https://www.linkedin.com/jobs/view/4321098765/") == "4321098765"


def test_extract_job_id_slug_and_query_forms():
    assert linkedin._extract_job_id(
        "https://www.linkedin.com/jobs/view/head-of-ai-at-acme-4321098765") == "4321098765"
    assert linkedin._extract_job_id(
        "https://www.linkedin.com/jobs/search/?currentJobId=4321098765") == "4321098765"


def test_extract_job_id_no_match():
    assert linkedin._extract_job_id("https://www.linkedin.com/jobs/search") is None
    assert linkedin._extract_job_id(None) is None
    assert linkedin._extract_job_id("") is None


# ---------------------------------------------------------------------------
# Closed-posting detection
# ---------------------------------------------------------------------------

def test_body_shows_closed():
    assert linkedin._body_shows_closed("<div>No longer accepting applications</div>")
    assert linkedin._body_shows_closed('<figure class="closed-job">x</figure>')
    assert linkedin._body_shows_closed("THIS JOB HAS EXPIRED")
    assert not linkedin._body_shows_closed("<div>Apply now</div>")
    assert not linkedin._body_shows_closed("")
    assert not linkedin._body_shows_closed(None)


def test_page_shows_closed():
    assert linkedin._page_shows_closed(_soup('<figcaption class="closed-job">Closed</figcaption>'))
    assert linkedin._page_shows_closed(_soup("<p>This job is no longer available</p>"))
    assert not linkedin._page_shows_closed(_soup("<p>Apply now</p>"))
    assert not linkedin._page_shows_closed(None)


def test_is_posting_closed_non_linkedin_is_unknown():
    assert linkedin.is_posting_closed("https://example.com/jobs/1") is None
    assert linkedin.is_posting_closed(None) is None


def test_is_posting_closed_fetch_paths(monkeypatch):
    url = "https://www.linkedin.com/jobs/view/4321098765"

    monkeypatch.setattr(linkedin, "_fetch", lambda u: (200, "<div>No longer accepting applications</div>"))
    assert linkedin.is_posting_closed(url) is True

    monkeypatch.setattr(linkedin, "_fetch", lambda u: (200, "<div>Apply now</div>"))
    assert linkedin.is_posting_closed(url) is False

    # Both endpoints failing -> unknown (None), never a prune signal.
    monkeypatch.setattr(linkedin, "_fetch", lambda u: (404, None))
    assert linkedin.is_posting_closed(url) is None


# ---------------------------------------------------------------------------
# Search-card parsing
# ---------------------------------------------------------------------------

CARDS_HTML = """
<div>
  <div data-entity-urn="urn:li:jobPosting:4321098765">
    <div class="base-search-card__info">
      <h3>Head of AI</h3>
      <a class="hidden-nested-link">Acme
BV</a>
      <span class="job-search-card__location">Amsterdam, North Holland</span>
    </div>
  </div>
  <div>
    <div class="base-search-card__info">
      <h3>Ghost Card</h3>
    </div>
  </div>
</div>
"""


def test_parse_cards():
    jobs = linkedin._parse_cards(_soup(CARDS_HTML))
    # The card without a data-entity-urn on its parent is skipped.
    assert jobs == [{
        "job_id": "4321098765",
        "title": "Head of AI",
        "company": "Acme BV",
        "location": "Amsterdam, North Holland",
    }]


def test_parse_cards_empty_page():
    assert linkedin._parse_cards(_soup("<html></html>")) == []
