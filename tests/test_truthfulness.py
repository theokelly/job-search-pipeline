"""Tests for the truthfulness gate: prompt building and fail-safe behavior."""
from job_agent.db import Job, Score
from job_agent.generation.truthfulness import (
    build_grounding_check_messages,
    check_grounding,
)


def _job() -> Job:
    return Job(
        id="x", source="manual", company="Acme", title="Data Lead",
        location="Amsterdam", url="https://example.com",
        description_raw="Data Lead role.", status="active",
    )


def _score() -> Score:
    return Score(
        job_id="x", user_id=1, tier="Strong Fit", skill_fit=85,
        preference_alignment=80, recommended_resume_version="ai_focused",
        rationale="r", gap_assessment="g", positioning_notes="p", raw_response="",
    )


def test_prompt_contains_letter_and_ground_truth():
    messages = build_grounding_check_messages(
        _job(), _score(),
        document="I led a team of 12 at Initech.",
        resume_text="Senior Analyst at Initech, team of 5.",
        starls_text="Grew pipeline reliability.",
        profile={},
    )
    text = messages[0]["content"]
    assert "I led a team of 12 at Initech." in text
    assert "team of 5" in text
    assert "**Verdict:** PASS | REVIEW" in text
    assert "When in doubt, flag it" in text


def test_check_never_raises(monkeypatch):
    """If the API call fails, check_grounding returns a REVIEW report."""
    import job_agent.generation.truthfulness as mod

    # check_grounding reads the key before constructing the client; without
    # one it short-circuits and Boom never runs (cleanroom CI has no key).
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    class Boom:
        def __init__(self, *a, **k):
            raise RuntimeError("no network")

    monkeypatch.setattr(mod.anthropic, "Anthropic", Boom)
    report = check_grounding(_job(), _score(), "letter", "resume", "starls", {})
    assert "**Verdict:** REVIEW" in report
    assert "no network" in report


def test_document_label_in_prompt():
    messages = build_grounding_check_messages(
        _job(), _score(),
        document="**Why:** strategy",
        resume_text="resume", starls_text="starls", profile={},
        document_label="Resume recommendation",
    )
    text = messages[0]["content"]
    assert "## Resume recommendation to verify" in text
    assert "in the resume recommendation" in text


def test_agent_stance_in_checker_rules():
    messages = build_grounding_check_messages(
        _job(), _score(),
        document="I deployed agentic tooling at FB Solutions.",
        resume_text="Analyst at Initech.",
        starls_text="",
        profile={},
    )
    text = messages[0]["content"]
    assert '"agentic"' in text
    assert "personal projects" in text
