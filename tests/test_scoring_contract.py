"""
Verifies the scoring prompt structure and schema — no live API calls.
"""
from datetime import datetime
from job_agent.db import Job, job_id
from job_agent.scoring.prompt import build_scoring_messages, SCORING_SCHEMA, SCORE_TOOL


def make_job() -> Job:
    return Job(
        id=job_id("TestCo", "ML Engineer", "Amsterdam"),
        source="greenhouse_api",
        company="TestCo",
        title="ML Engineer",
        location="Amsterdam",
        url="https://example.com/job",
        description_raw="We are looking for an ML Engineer with 3+ years Python experience.",
        first_seen=datetime.utcnow(),
        last_seen=datetime.utcnow(),
    )


PROFILE = {
    "scoring": {
        "skill_fit_signals": {
            "strong_positive": ["Python", "data infrastructure"],
            "positive": ["strategy"],
            "negative": ["production engineering"],
            "blocking": ["Dutch required"],
        },
        "preference_alignment_signals": {
            "strong_positive": ["AI-forward employer"],
            "positive": ["hybrid"],
            "negative": ["early-stage startup"],
        },
        "tier_definitions": {
            "Strong Fit": "Can do it day one, aligns with preferences.",
            "Worth Reviewing": "One dimension clear, other borderline.",
            "Long Shot": "Meaningful gaps but worth a look.",
            "Skip": "Clear mismatch.",
        },
        "honesty_calibration": ["Do not paper over gaps."],
    },
    "not_targeting": ["Pure SWE roles"],
}

NARRATIVE = "Theo is a data scientist pivoting to AI product roles in Amsterdam."


def test_messages_structure():
    job = make_job()
    messages = build_scoring_messages(job, PROFILE, NARRATIVE)
    assert len(messages) == 1
    assert messages[0]["role"] == "user"
    content = messages[0]["content"]
    assert isinstance(content, list)
    assert len(content) == 3


def test_first_two_blocks_are_cached():
    job = make_job()
    messages = build_scoring_messages(job, PROFILE, NARRATIVE)
    content = messages[0]["content"]
    assert content[0].get("cache_control") == {"type": "ephemeral"}
    assert content[1].get("cache_control") == {"type": "ephemeral"}
    assert "cache_control" not in content[2]


def test_prompt_contains_job_fields():
    job = make_job()
    messages = build_scoring_messages(job, PROFILE, NARRATIVE)
    all_text = " ".join(
        block["text"] for block in messages[0]["content"] if isinstance(block, dict)
    )
    assert "TestCo" in all_text
    assert "ML Engineer" in all_text
    assert "Amsterdam" in all_text


def test_prompt_contains_narrative():
    job = make_job()
    messages = build_scoring_messages(job, PROFILE, NARRATIVE)
    all_text = " ".join(b["text"] for b in messages[0]["content"])
    assert "Theo" in all_text


def test_schema_has_required_keys():
    required = SCORING_SCHEMA["required"]
    for key in ["tier", "skill_fit", "preference_alignment", "recommended_resume_version",
                "gap_assessment", "positioning_notes", "rationale"]:
        assert key in required


def test_tier_enum_values():
    tier_enum = SCORE_TOOL["input_schema"]["properties"]["tier"]["enum"]
    assert "Strong Fit" in tier_enum
    assert "Worth Reviewing" in tier_enum
    assert "Long Shot" in tier_enum
    assert "Skip" in tier_enum
