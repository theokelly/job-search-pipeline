"""Tests for the Ashby discovery connector: normalization and fetching."""
from job_agent.discovery.normalizer import normalize_ashby
from job_agent.discovery import ashby


RAW = {
    "id": "abc123",
    "title": "Senior Data Scientist",
    "departmentName": "Data",
    "locationName": "Amsterdam, Netherlands",
    "workplaceType": "hybrid",
    "isRemote": False,
    "descriptionHtml": "<h2>About the role</h2><p>We need a data scientist.</p>",
    "jobUrl": "https://jobs.ashbyhq.com/acme/abc123",
    "publishedAt": "2026-10-01T00:00:00Z",
}


def test_normalize_ashby():
    job = normalize_ashby("Acme", RAW)
    assert job.company == "Acme"
    assert job.title == "Senior Data Scientist"
    assert job.location == "Amsterdam, Netherlands"
    assert job.url == "https://jobs.ashbyhq.com/acme/abc123"
    assert job.source == "ashby_api"
    assert job.status == "active"
    assert "data scientist" in job.description_raw


def test_normalize_ashby_remote_location():
    raw = dict(RAW, locationName="", isRemote=True)
    job = normalize_ashby("Acme", raw)
    assert "remote" in job.location.lower()


def test_normalize_ashby_missing_fields():
    job = normalize_ashby("Acme", {"title": "Analyst"})
    assert job.title == "Analyst"
    assert job.location == ""
    assert job.url == ""
    assert job.description_raw == ""


def test_fetch_jobs_parses_board(monkeypatch):
    calls = {}

    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"jobs": [RAW, RAW]}

    def fake_get(url, timeout=None):
        calls["url"] = url
        return FakeResp()

    monkeypatch.setattr(ashby.httpx, "get", fake_get)
    jobs = ashby.fetch_jobs("Acme", "acme")
    assert calls["url"] == "https://api.ashbyhq.com/posting-api/job-board/acme"
    assert len(jobs) == 2
    assert all(j.source == "ashby_api" for j in jobs)
