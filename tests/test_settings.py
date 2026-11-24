"""Characterization tests for job_agent.settings (Phase C).

settings.py is pure functions over a profile dict; these tests pin the
default values, the profile-override merge semantics, and the geography
fallback behavior so later refactors can't drift them silently.
"""
from job_agent import settings
from job_agent.tiers import STRONG_FIT, WORTH_REVIEWING


# ---------------------------------------------------------------------------
# model_for
# ---------------------------------------------------------------------------

def test_model_defaults_on_empty_profile():
    assert settings.model_for({}, "prescreen") == "claude-haiku-5-5"
    assert settings.model_for({}, "score") == "claude-sonnet-5-5"
    assert settings.model_for({}, "resume_recommendation") == "claude-haiku-5-5"
    assert settings.model_for({}, "application_answers") == "claude-haiku-5-5"
    assert settings.model_for({}, "truthfulness_check") == "claude-haiku-5-5"


def test_model_profile_override_wins():
    profile = {"models": {"score": "custom-model"}}
    assert settings.model_for(profile, "score") == "custom-model"
    # Untouched tasks still fall back to defaults.
    assert settings.model_for(profile, "prescreen") == "claude-haiku-5-5"


def test_cover_letter_model_by_tier():
    assert settings.model_for({}, "cover_letter", tier=STRONG_FIT) == "claude-opus-5-5"
    assert settings.model_for({}, "cover_letter", tier=WORTH_REVIEWING) == "claude-sonnet-5-5"


def test_cover_letter_model_unknown_tier_uses_default():
    assert settings.model_for({}, "cover_letter", tier="Long Shot") == "claude-sonnet-5-5"
    assert settings.model_for({}, "cover_letter") == "claude-sonnet-5-5"


def test_cover_letter_model_profile_by_tier():
    profile = {"models": {"cover_letter_by_tier": {STRONG_FIT: "opus-custom"}}}
    assert settings.model_for(profile, "cover_letter", tier=STRONG_FIT) == "opus-custom"


# ---------------------------------------------------------------------------
# truncation / section merging
# ---------------------------------------------------------------------------

def test_truncation_defaults_and_override():
    assert settings.truncation({}, "prescreen_jd_chars") == 1000
    assert settings.truncation({}, "score_jd_chars") == 4500
    profile = {"truncation": {"prescreen_jd_chars": 4500}}
    assert settings.truncation(profile, "prescreen_jd_chars") == 4500


def test_truncation_unknown_key_falls_back_to_4000():
    assert settings.truncation({}, "no_such_key") == 4000


def test_pipeline_and_batches_config_merge():
    pc = settings.pipeline_config({})
    assert pc["db_path"] == "data/jobs.sqlite"
    assert pc["expire_after_days"] == 14
    pc2 = settings.pipeline_config({"pipeline": {"expire_after_days": 30}})
    assert pc2["expire_after_days"] == 30
    assert pc2["db_path"] == "data/jobs.sqlite"  # default survives partial override
    bc = settings.batches_config({})
    assert bc["poll_timeout_seconds"] == 600
    assert bc["enabled_by_default"] is True


def test_fanout_config_defaults():
    fc = settings.fanout_config({})
    assert fc["enabled_by_default"] is False
    assert fc["provider"] == "brave"
    assert "jobs.ashbyhq.com" in fc["domains"]


# ---------------------------------------------------------------------------
# prescreen_config
# ---------------------------------------------------------------------------

def test_prescreen_config_defaults_empty():
    cfg = settings.prescreen_config({})
    assert cfg == {"persona": "", "geography_rules": "", "reject_signals": []}


def test_prescreen_config_strips_and_passes_signals():
    profile = {"prescreen": {
        "persona": "  senior leader  ",
        "geography_rules": " Amsterdam only ",
        "reject_signals": [{"name": "ic", "pattern": "hands-on"}],
    }}
    cfg = settings.prescreen_config(profile)
    assert cfg["persona"] == "senior leader"
    assert cfg["geography_rules"] == "Amsterdam only"
    assert cfg["reject_signals"][0]["name"] == "ic"


# ---------------------------------------------------------------------------
# Geography helpers
# ---------------------------------------------------------------------------

def test_geography_fallbacks_without_profile_block():
    eu = settings.eu_place_terms({})
    assert "amsterdam" in eu and "netherlands" in eu and "europe" in eu
    assert settings.remote_signals({}) == {"remote", "hybrid", "eu"}
    non_eu = settings.non_eu_terms({})
    assert "united states" in non_eu and "california" in non_eu


def test_geography_profile_terms_lowercased():
    profile = {"geography": {
        "eu_countries": {"Netherlands": ["Amsterdam", "UTRECHT"]},
        "remote_signals": ["Remote", "HYBRID"],
        "non_eu_terms": ["Canada"],
    }}
    assert settings.eu_place_terms(profile) == {"amsterdam", "utrecht"}
    assert settings.remote_signals(profile) == {"remote", "hybrid"}
    assert settings.non_eu_terms(profile) == {"canada"}


def test_eu_allowlist_is_union_of_places_and_remote_signals():
    allow = settings.eu_allowlist_terms({})
    assert "amsterdam" in allow and "remote" in allow
