import pytest
from job_agent.config import ProfileError, validate_profile


def test_valid_minimal_profile():
    p = {
        "geography": {"target_city": "amsterdam", "target_country": "netherlands"},
        "pre_filter_terms": ["gambling"],
        "title_filter_terms": ["account executive"],
        "models": {"score": "claude-sonnet-4-6"},
        "pipeline": {"expire_after_days": 14, "auto_sweep_on_run": True},
        "linkedin_search_queries": [{"keywords": "data analyst", "location": "Amsterdam"}],
    }
    assert validate_profile(p) is p


def test_empty_profile_ok():
    assert validate_profile({}) == {}


def test_none_profile_fails():
    with pytest.raises(ProfileError):
        validate_profile(None)


def test_wrong_section_type_fails():
    with pytest.raises(ProfileError) as ei:
        validate_profile({"geography": "amsterdam"})
    assert "'geography' must be a mapping" in str(ei.value)


def test_wrong_value_type_fails():
    with pytest.raises(ProfileError) as ei:
        validate_profile({"pipeline": {"expire_after_days": "14"}})
    assert "'pipeline.expire_after_days' must be int" in str(ei.value)


def test_bool_is_not_int():
    with pytest.raises(ProfileError):
        validate_profile({"pipeline": {"expire_after_days": True}})


def test_str_list_with_non_str_fails():
    with pytest.raises(ProfileError):
        validate_profile({"pre_filter_terms": ["gambling", 42]})


def test_bad_query_fails():
    with pytest.raises(ProfileError) as ei:
        validate_profile({"linkedin_search_queries": [{"location": "Amsterdam"}]})
    assert "missing required 'keywords'" in str(ei.value)


def test_truncation_values_must_be_int():
    with pytest.raises(ProfileError):
        validate_profile({"truncation": {"score_jd_chars": "4500"}})


def test_unknown_keys_warn_but_pass(capsys):
    p = {"titel_filter_terms": ["x"], "geography": {"target_ctype": "amsterdam"}}
    assert validate_profile(p) is p
    err = capsys.readouterr().err
    assert "titel_filter_terms" in err
    assert "target_ctype" in err


def test_multiple_errors_all_reported():
    with pytest.raises(ProfileError) as ei:
        validate_profile({
            "geography": {"target_city": 123},
            "batches": {"poll_timeout_seconds": "600"},
        })
    msg = str(ei.value)
    assert "'geography.target_city' must be str" in msg
    assert "'batches.poll_timeout_seconds' must be int" in msg


def test_target_companies_with_nulls_ok():
    p = {"target_companies": [{
        "name": "Adyen", "website": None, "city": "Amsterdam",
        "country": "Netherlands", "ats_type": None,
        "greenhouse_token": None, "lever_slug": None,
        "priority": "high", "notes": "",
    }]}
    assert validate_profile(p) is p


def test_target_companies_missing_name_tolerated():
    # discover.py uses company.get("name", "") — missing name doesn't crash
    p = {"target_companies": [{"website": "x.com", "greenhouse_token": None}]}
    assert validate_profile(p) is p


def test_target_roles_nested():
    p = {"target_roles": [{
        "priority": 1, "category": "data", "description": "d",
        "example_titles": ["Data Analyst"], "rationale": "r",
    }]}
    assert validate_profile(p) is p
    with pytest.raises(ProfileError):
        validate_profile({"target_roles": [{"priority": "1"}]})


def test_compensation_nulls_ok():
    p = {"compensation": {"target_base_eur": None, "target_total_eur": None, "notes": ""}}
    assert validate_profile(p) is p
    with pytest.raises(ProfileError):
        validate_profile({"compensation": {"target_base_eur": "100k"}})


def test_nested_company_preferences():
    p = {"company_preferences": {
        "remote_culture": {"in_office_ok": True, "hybrid_preferred": False,
                             "full_remote_ok_if_emea": True},
        "operating_model": {"pe_backed_relevant": False, "notes": ""},
    }}
    assert validate_profile(p) is p
    with pytest.raises(ProfileError):
        validate_profile({"company_preferences": {"remote_culture": {"in_office_ok": "yes"}}})
