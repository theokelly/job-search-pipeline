"""Characterization tests for discovery/email_alerts.py parsers (Phase C).

Body extraction, HTML stripping, and job-URL extraction from alert
emails — offline, with hand-built message fixtures.
"""
import email
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from job_agent.discovery import email_alerts


# ---------------------------------------------------------------------------
# Body extraction
# ---------------------------------------------------------------------------

def test_get_body_multipart_prefers_first_of_each_type():
    msg = MIMEMultipart("alternative")
    msg.attach(MIMEText("plain body", "plain"))
    msg.attach(MIMEText("<p>html body</p>", "html"))
    plain, html = email_alerts._get_body(msg)
    assert plain == "plain body"
    assert html == "<p>html body</p>"


def test_get_body_single_part():
    plain_msg = email.message_from_string(
        "Content-Type: text/plain; charset=utf-8\n\njust text")
    assert email_alerts._get_body(plain_msg) == ("just text", "")

    html_msg = email.message_from_string(
        "Content-Type: text/html; charset=utf-8\n\n<p>hi</p>")
    assert email_alerts._get_body(html_msg) == ("", "<p>hi</p>")


# ---------------------------------------------------------------------------
# HTML stripping
# ---------------------------------------------------------------------------

def test_strip_html():
    assert email_alerts._strip_html("<p>Hello <b>world</b></p>") == "Hello world"
    assert email_alerts._strip_html("Fish &amp; Chips") == "Fish & Chips"
    assert email_alerts._strip_html("a\n\n\n\nb") == "a\n\nb"
    assert email_alerts._strip_html("  spaced   out  ") == "spaced out"


# ---------------------------------------------------------------------------
# URL extraction
# ---------------------------------------------------------------------------

def test_extract_linkedin_urls_reconstructed_and_deduped():
    html = ('<a href="https://www.linkedin.com/comm/jobs/view/1111111111?trk=x">a</a>'
            '<a href="https://www.linkedin.com/comm/jobs/view/2222222222">b</a>'
            '<a href="https://www.linkedin.com/comm/jobs/view/1111111111">dup</a>')
    assert email_alerts._extract_job_urls(html, "linkedin") == [
        "https://www.linkedin.com/jobs/view/1111111111",
        "https://www.linkedin.com/jobs/view/2222222222",
    ]


def test_extract_indeed_and_glassdoor_urls():
    html = ('<a href="https://nl.indeed.com/viewjob?jk=abc123">i1</a>'
            '<a href="https://www.glassdoor.com/job-listing/head-of-ai-acme-JV_IC123.htm">g1</a>'
            '<a href="https://nl.indeed.com/viewjob?jk=abc123">dup</a>'
            '<a href="https://example.com/not-a-job-board">x</a>')
    urls = email_alerts._extract_job_urls(html, "indeed")
    assert urls == [
        "https://nl.indeed.com/viewjob?jk=abc123",
        "https://www.glassdoor.com/job-listing/head-of-ai-acme-JV_IC123.htm",
    ]


def test_extract_linkedin_jobs_from_html():
    html = ('<a href="https://www.linkedin.com/comm/jobs/view/1111111111">One</a>'
            '<a href="https://www.linkedin.com/comm/jobs/view/1111111111">Dup</a>'
            '<a href="https://www.linkedin.com/comm/jobs/view/2222222222">Two</a>'
            '<a href="https://www.linkedin.com/company/acme">Not a job</a>')
    jobs = email_alerts._extract_linkedin_jobs_from_html(html)
    assert [j["_linkedin_id"] for j in jobs] == ["1111111111", "2222222222"]
    assert jobs[0]["url"] == "https://www.linkedin.com/jobs/view/1111111111"
    # Titles/companies are deliberately left blank — fetched per-job later.
    assert jobs[0]["title"] == "" and jobs[0]["company"] == ""
