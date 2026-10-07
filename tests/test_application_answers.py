"""Tests for the application-answers generator: known-answer merging and prompt building."""
from job_agent.db import Job
from job_agent.generation.application_answers import (
    STANDARD_QUESTIONS,
    _known_answers,
    build_application_answers_messages,
)
from job_agent.config import validate_profile


def _job() -> Job:
    return Job(
        id="x", source="manual", company="Acme", title="Data Lead",
        location="Amsterdam", url="https://example.com",
        description_raw="We are hiring a Data Lead. Screening: do you have dbt experience?",
        status="active",
    )


def test_standard_question_keys_match_config_spec():
    """Every STANDARD_QUESTIONS key must be a valid application_answers yaml key."""
    profile = validate_profile({
        "application_answers": {key: "test" for key, _ in STANDARD_QUESTIONS},
    })
    assert set(profile["application_answers"]) == {key for key, _ in STANDARD_QUESTIONS}


def test_known_answers_defaults_and_overrides():
    # Empty profile: pipeline-known facts fill in
    answers = _known_answers({})
    assert answers["location"] == "Amsterdam, Netherlands"
    assert "EU citizen" in answers["work_authorization"]
    assert "No" in answers["visa_sponsorship"]
    # User yaml wins over defaults
    answers = _known_answers({"application_answers": {"location": "Rotterdam, Netherlands"}})
    assert answers["location"] == "Rotterdam, Netherlands"
    # Empty-string yaml values are treated as unanswered
    answers = _known_answers({"application_answers": {"salary_expectation": ""}})
    assert not answers.get("salary_expectation")


def test_prompt_marks_missing_answers_todo():
    profile = {"application_answers": {"salary_expectation": "EUR 120k"}}
    messages = build_application_answers_messages(_job(), profile, "resume text")
    text = messages[0]["content"]
    assert "notice_period" in text  # listed as not provided
    assert "TODO" in text
    assert "EUR 120k" in text  # known answer passed through verbatim


def test_prompt_extracts_job_screening_questions():
    profile = {}
    messages = build_application_answers_messages(_job(), profile, "resume text")
    text = messages[0]["content"]
    assert "dbt experience" in text  # JD surfaced for job-specific questions


def test_prompt_never_asks_model_to_invent():
    messages = build_application_answers_messages(_job(), {}, "resume text")
    text = messages[0]["content"]
    assert "Never invent" in text
