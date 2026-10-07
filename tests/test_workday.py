"""Tests for the Workday discovery connector: board parsing, normalization, fetching."""
from job_agent.discovery.normalizer import normalize_workday
from job_agent.discovery import workday


POSTING = {
    "title": "Head of Analytics",
    "locationsText": "Amsterdam, Netherlands",
    "externalPath": "/job/Amsterdam/Head-of-Analytics_12345",
    "bulletFields": ["12345"],
    "postedOn": "Posted Today",
}

DETAIL = {
    "jobPostingInfo": {
        "title": "Head of Analytics",
        "jobDescription": "<p>Lead our analytics org.</p>",
        "location": "Amsterdam",
        "jobReqId": "12345",
    }
}


def test_split_board():
    host, tenant, site = workday._split_board(
        "philips.wd3.myworkdayjobs.com/jobs-and-careers")
    assert host == "philips.wd3.myworkdayjobs.com"
    assert tenant == "philips"
    assert site == "jobs-and-careers"


def test_normalize_workday():
    job = normalize_workday("Philips", "philips.wd3.myworkdayjobs.com",
                            "jobs-and-careers", POSTING, DETAIL)
    assert job.company == "Philips"
    assert job.title == "Head of Analytics"
    assert job.location == "Amsterdam, Netherlands"
    assert job.source == "workday_api"
    assert job.status == "active"
    assert "Lead our analytics org" in job.description_raw
    assert job.url.startswith("https://philips.wd3.myworkdayjobs.com/jobs-and-careers/job/")


def test_normalize_workday_missing_detail():
    job = normalize_workday("Philips", "h", "s", POSTING, {})
    assert job.description_raw == ""
    assert job.title == "Head of Analytics"


def test_fetch_jobs_unions_queries_and_dedupes(monkeypatch):
    calls = {"posts": [], "gets": []}

    class FakeResp:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            pass

        def json(self):
            return self._payload

    def fake_post(url, json=None, headers=None, timeout=None):
        calls["posts"].append((url, json["searchText"]))
        return FakeResp({"total": 1, "jobPostings": [POSTING]})

    def fake_get(url, headers=None, timeout=None):
        calls["gets"].append(url)
        return FakeResp(DETAIL)

    monkeypatch.setattr(workday.httpx, "post", fake_post)
    monkeypatch.setattr(workday.httpx, "get", fake_get)

    jobs = workday.fetch_jobs(
        "Philips", "philips.wd3.myworkdayjobs.com/jobs-and-careers",
        queries=["analytics", "data"])
    # Same posting returned for both queries -> deduped to one job, one detail fetch
    assert len(jobs) == 1
    assert len(calls["gets"]) == 1
    assert jobs[0].source == "workday_api"
    assert any("wday/cxs/philips/jobs-and-careers/jobs" in u for u, _ in calls["posts"])
