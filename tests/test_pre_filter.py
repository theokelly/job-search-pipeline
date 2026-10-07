from datetime import datetime
from job_agent.db import Job, job_id
from job_agent.filter import pre_filter


def make_job(title="Data Analyst", location="Amsterdam", description="") -> Job:
    return Job(
        id=job_id("Acme", title, location),
        source="greenhouse_api",
        company="Acme",
        title=title,
        location=location,
        url="https://example.com",
        description_raw=description,
        first_seen=datetime.utcnow(),
        last_seen=datetime.utcnow(),
    )


PROFILE = {
    "geography": {
        "target_city": "amsterdam",
        "target_country": "netherlands",
        "open_to_remote_eu": True,
        "open_to_eu_based": True,
    },
    "pre_filter_terms": ["gambling"],
    "title_filter_terms": ["account executive", "security engineer"],
}


def test_passes_matching_location():
    job = make_job(location="Amsterdam, Netherlands")
    keep, reason = pre_filter(job, PROFILE)
    assert keep


def test_fails_wrong_location():
    job = make_job(location="New York, USA")
    keep, reason = pre_filter(job, PROFILE)
    assert not keep
    assert "location" in reason


def test_remote_passes_location_filter():
    job = make_job(location="Remote")
    keep, reason = pre_filter(job, PROFILE)
    assert keep


def test_dealbreaker_in_title():
    job = make_job(title="Gambling Operations Manager")
    keep, reason = pre_filter(job, PROFILE)
    assert not keep
    assert "gambling" in reason


def test_dealbreaker_in_description():
    job = make_job(description="This role involves gambling operations.")
    keep, reason = pre_filter(job, PROFILE)
    assert not keep
    assert "gambling" in reason


def test_no_dealbreakers_no_location_filter():
    job = make_job(location="Anywhere")
    keep, _ = pre_filter(job, {})
    assert keep


def test_eu_remote_passes():
    job = make_job(location="Remote - Netherlands")
    keep, _ = pre_filter(job, PROFILE)
    assert keep


def test_non_eu_remote_dropped():
    for loc in ["Remote - California", "Remote - India", "Remote - Texas"]:
        job = make_job(location=loc)
        keep, reason = pre_filter(job, PROFILE)
        assert not keep, f"Expected {loc} to be dropped"
        # Either drop message indicates the same outcome:
        # "location outside EU/UK" (matched a non-EU term) or "location mismatch" (no EU match)
        assert "location" in reason


def test_eu_remote_no_country_qualifier_passes():
    # "Remote" with no qualifier — benefit of the doubt
    job = make_job(location="Remote")
    keep, _ = pre_filter(job, PROFILE)
    assert keep


def test_title_filter_blocks_ae():
    job = make_job(title="Strategic Account Executive, Financial Services")
    keep, reason = pre_filter(job, PROFILE)
    assert not keep
    assert "account executive" in reason


def test_title_filter_does_not_match_description():
    # "account executive" in description only should NOT trigger title filter
    job = make_job(title="Senior Analytics Manager", description="You will partner with account executives.")
    keep, _ = pre_filter(job, PROFILE)
    assert keep


def test_multi_location_with_amsterdam_passes():
    job = make_job(location="Amsterdam, Netherlands; Berlin, Germany; London, United Kingdom")
    keep, _ = pre_filter(job, PROFILE)
    assert keep


# ---------------------------------------------------------------------------
# Commute mode (geography.commutable_terms set): Amsterdam commute or remote only
# ---------------------------------------------------------------------------

COMMUTE_PROFILE = {
    "geography": {
        "target_city": "amsterdam",
        "target_country": "netherlands",
        "commutable_terms": ["amsterdam", "amstelveen", "hoofddorp", "haarlem", "almere", "utrecht"],
        "ambiguous_locations": ["netherlands", ""],
        "remote_location_signals": ["remote", "emea", "europe"],
        "jd_remote_patterns": [r"\bremote\b", "work from anywhere"],
        "jd_min_chars_for_remote_check": 300,
    },
    "title_filter_terms": ["account executive"],
}

ONSITE_JD = "You will lead our analytics team from our office. " * 20
REMOTE_JD = "This is a fully remote role open to candidates across EMEA. " * 20


def test_commute_amsterdam_passes():
    for loc in ["Amsterdam, North Holland, Netherlands", "Amsterdam Area", "Hoofddorp, Netherlands",
                "Utrecht, Netherlands", "Almere, Flevoland, Netherlands"]:
        keep, _ = pre_filter(make_job(location=loc, description=ONSITE_JD), COMMUTE_PROFILE)
        assert keep, loc


def test_commute_onsite_london_dropped():
    keep, reason = pre_filter(make_job(location="London, England, United Kingdom", description=ONSITE_JD), COMMUTE_PROFILE)
    assert not keep
    assert "commuting range" in reason


def test_commute_onsite_other_dutch_city_dropped():
    keep, _ = pre_filter(make_job(location="Rotterdam, South Holland, Netherlands", description=ONSITE_JD), COMMUTE_PROFILE)
    assert not keep


def test_commute_london_with_remote_jd_passes_to_prescreen():
    keep, _ = pre_filter(make_job(location="London, England, United Kingdom", description=REMOTE_JD), COMMUTE_PROFILE)
    assert keep


def test_commute_remote_location_passes():
    for loc in ["Remote", "Remote - EMEA", "Europe", "Berlin, Germany (Remote)"]:
        keep, _ = pre_filter(make_job(location=loc, description=ONSITE_JD), COMMUTE_PROFILE)
        assert keep, loc


def test_commute_ambiguous_netherlands_passes():
    keep, _ = pre_filter(make_job(location="Netherlands", description=ONSITE_JD), COMMUTE_PROFILE)
    assert keep


def test_commute_short_jd_benefit_of_doubt():
    keep, _ = pre_filter(make_job(location="Berlin, Germany", description="Short card."), COMMUTE_PROFILE)
    assert keep


def test_commute_non_eu_dropped_even_with_remote_jd():
    keep, reason = pre_filter(make_job(location="New York, USA", description=REMOTE_JD), COMMUTE_PROFILE)
    assert not keep
    assert "outside EU" in reason


def test_commute_multi_location_with_amsterdam_passes():
    keep, _ = pre_filter(make_job(location="London, United Kingdom; Amsterdam, Netherlands", description=ONSITE_JD), COMMUTE_PROFILE)
    assert keep


def test_commute_country_only_passes_to_prescreen():
    prof = {**COMMUTE_PROFILE, "geography": {**COMMUTE_PROFILE["geography"],
            "eu_countries": {"united_kingdom": ["united kingdom", "london"], "germany": ["germany", "berlin"]}}}
    keep, _ = pre_filter(make_job(location="United Kingdom", description=ONSITE_JD), prof)
    assert keep
    keep, _ = pre_filter(make_job(location="London, United Kingdom", description=ONSITE_JD), prof)
    assert not keep
