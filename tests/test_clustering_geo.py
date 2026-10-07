from datetime import datetime
from job_agent.db import Job, job_id
from job_agent.clustering import geo_priority, pick_canonical

PROFILE = {
    "geography": {
        "commutable_terms": ["amsterdam", "utrecht", "hoofddorp"],
        "remote_location_signals": ["remote", "emea", "europe"],
    }
}


def _job(location, desc="x" * 100):
    return Job(
        id=job_id("Acme", "Head of AI", location), source="linkedin", company="Acme",
        title="Head of AI", location=location, url="https://example.com",
        description_raw=desc, first_seen=datetime.utcnow(), last_seen=datetime.utcnow(),
    )


def test_priority_order():
    ams = geo_priority(_job("Amsterdam, Netherlands"), PROFILE)
    remote = geo_priority(_job("Remote - EMEA"), PROFILE)
    rotterdam = geo_priority(_job("Rotterdam, Netherlands"), PROFILE)
    london = geo_priority(_job("London, United Kingdom"), PROFILE)
    assert ams < remote < rotterdam < london


def test_canonical_prefers_amsterdam_over_london():
    jobs = [_job("London, United Kingdom", desc="y" * 5000), _job("Amsterdam, Netherlands")]
    assert pick_canonical(jobs, PROFILE).location.startswith("Amsterdam")
