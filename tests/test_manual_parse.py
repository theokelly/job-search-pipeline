"""Characterization tests for discovery/manual.py parsers (Phase C).

Fixture HTML only — no network. fetch_from_url is exercised with its
_fetch_html seam monkeypatched; the LinkedIn stage is neutralized the
same way so the JSON-LD / __NEXT_DATA__ cascade order is what's tested.
"""
import json

import pytest
from bs4 import BeautifulSoup

from job_agent.discovery import manual


def _soup(html):
    return BeautifulSoup(html, "html.parser")


JSONLD_PAGE = """
<html><head>
<script type="application/ld+json">
{"@type": "JobPosting", "title": "Head of AI",
 "hiringOrganization": {"@type": "Organization", "name": "Acme BV"},
 "jobLocation": {"@type": "Place", "address": {"@type": "PostalAddress",
   "addressLocality": "Amsterdam", "addressRegion": "NH", "addressCountry": "NL"}},
 "description": "<p>Lead the <b>AI</b> team.</p>"}
</script>
</head><body></body></html>
"""


# ---------------------------------------------------------------------------
# JSON-LD stage
# ---------------------------------------------------------------------------

def test_jsonld_extracts_fields():
    out = manual._extract_jsonld(_soup(JSONLD_PAGE))
    assert out["title"] == "Head of AI"
    assert out["company"] == "Acme BV"
    assert out["location"] == "Amsterdam, NH, NL"
    # _strip_html joins element text with newlines (get_text("\n")) —
    # pinned as-is; stored JDs carry this shape today.
    assert out["description"] == "Lead the\nAI\nteam."


def test_jsonld_graph_and_list_wrappers():
    graph = {"@graph": [{"@type": "WebPage"}, {"@type": "JobPosting", "title": "T2",
             "hiringOrganization": "StringOrg", "description": "d"}]}
    page = f'<script type="application/ld+json">{json.dumps(graph)}</script>'
    out = manual._extract_jsonld(_soup(page))
    assert out["title"] == "T2"
    assert out["company"] == "StringOrg"  # string org form

    listed = [{"@type": "JobPosting", "title": "T3", "description": "d"}]
    page = f'<script type="application/ld+json">{json.dumps(listed)}</script>'
    assert manual._extract_jsonld(_soup(page))["title"] == "T3"


def test_jsonld_ignores_non_postings_and_bad_json():
    page = ('<script type="application/ld+json">{"@type": "WebPage"}</script>'
            '<script type="application/ld+json">{broken</script>')
    out = manual._extract_jsonld(_soup(page))
    assert out == manual._EMPTY


def test_location_from_jsonld_variants():
    assert manual._location_from_jsonld({}) == ""
    assert manual._location_from_jsonld({"jobLocation": "Amsterdam"}) == "Amsterdam"
    multi = {"jobLocation": [
        {"address": {"addressLocality": "Amsterdam"}},
        {"address": {"addressLocality": "Utrecht"}},
        {"name": ""},
    ]}
    assert manual._location_from_jsonld(multi) == "Amsterdam; Utrecht"
    named = {"jobLocation": {"name": "Remote EU"}}
    assert manual._location_from_jsonld(named) == "Remote EU"


# ---------------------------------------------------------------------------
# __NEXT_DATA__ stage (Greenhouse-shaped)
# ---------------------------------------------------------------------------

def _next_page(payload):
    return f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(payload)}</script>'


def _gh_job(jid, title):
    return {"id": jid, "title": title, "company_name": "Vinted",
            "location": {"name": "Amsterdam"}, "content": "<p>About the role</p>"}


def test_next_data_canonical_slot():
    payload = {"props": {"pageProps": {"job": {"data": _gh_job(1234567, "AI Lead")}}}}
    out = manual._extract_next_data(_soup(_next_page(payload)), "https://x.nl/jobs/1234567")
    assert out["title"] == "AI Lead"
    assert out["company"] == "Vinted"
    assert out["location"] == "Amsterdam"
    assert out["description"] == "About the role"


def test_next_data_picks_url_id_among_many():
    payload = {"props": {"pageProps": {"related": [_gh_job(1111111, "Wrong"),
                                                   _gh_job(2222222, "Right")]}}}
    out = manual._extract_next_data(_soup(_next_page(payload)), "https://x.nl/jobs/2222222")
    assert out["title"] == "Right"


def test_next_data_ambiguous_without_url_id_returns_empty():
    payload = {"props": {"pageProps": {"related": [_gh_job(1111111, "A"),
                                                   _gh_job(2222222, "B")]}}}
    out = manual._extract_next_data(_soup(_next_page(payload)), "https://x.nl/jobs")
    assert out["title"] == ""


def test_next_data_missing_or_invalid():
    assert manual._extract_next_data(_soup("<html></html>"), "https://x.nl")["title"] == ""
    bad = '<script id="__NEXT_DATA__">{nope</script>'
    assert manual._extract_next_data(_soup(bad), "https://x.nl")["title"] == ""


# ---------------------------------------------------------------------------
# Helpers + cascade
# ---------------------------------------------------------------------------

def test_strip_html_variants():
    assert manual._strip_html("plain") == "plain"
    assert manual._strip_html("<li>one</li><li>two</li>") == "one\ntwo"
    assert manual._strip_html("&lt;p&gt;encoded&lt;/p&gt;") == "encoded"
    assert manual._strip_html("&amp;lt;p&amp;gt;double&amp;lt;/p&amp;gt;") == "double"


def test_merge_right_wins_for_non_empty():
    out = manual._merge({"a": "x", "b": ""}, {"a": "", "b": "y"}, {"a": "z"})
    assert out == {"a": "z", "b": "y"}
    # Empty values never override, but do fill missing keys.
    assert manual._merge({}, {"a": ""}) == {"a": ""}


def test_fetch_from_url_empty_url():
    assert manual.fetch_from_url("   ") == manual._EMPTY


def test_fetch_from_url_cascade_prefers_jsonld(monkeypatch):
    monkeypatch.setattr(manual, "_extract_linkedin", lambda url: dict(manual._EMPTY))
    monkeypatch.setattr(manual, "_fetch_html", lambda url: _soup(JSONLD_PAGE))
    out = manual.fetch_from_url("https://example.com/job/1")
    assert out["title"] == "Head of AI"
    assert out["url"] == "https://example.com/job/1"


def test_fetch_from_url_fetch_failure_returns_url_only(monkeypatch):
    monkeypatch.setattr(manual, "_extract_linkedin", lambda url: dict(manual._EMPTY))
    monkeypatch.setattr(manual, "_fetch_html", lambda url: None)
    out = manual.fetch_from_url("https://example.com/job/1")
    assert out["title"] == ""
    assert out["url"] == "https://example.com/job/1"
