from ..db import Job
from .. import settings

# Tool schema used with Anthropic tool_use for structured output.
SCORE_TOOL = {
    "name": "record_score",
    "description": "Record the structured fit assessment for a job posting.",
    "input_schema": {
        "type": "object",
        "properties": {
            "tier": {
                "type": "string",
                "enum": ["Strong Fit", "Worth Reviewing", "Long Shot", "Skip"],
                "description": "Overall fit tier.",
            },
            "skill_fit": {
                "type": "integer",
                "minimum": 0,
                "maximum": 100,
                "description": "How well the candidate's skills match the role requirements (0-100).",
            },
            "preference_alignment": {
                "type": "integer",
                "minimum": 0,
                "maximum": 100,
                "description": "How well the role matches the candidate's stated preferences (0-100).",
            },
            "recommended_resume_version": {
                "type": "string",
                "enum": ["ai_focused", "analytics", "strategy"],
                "description": "Which resume version to lead with for this application.",
            },
            "gap_assessment": {
                "type": "string",
                "description": "2-4 sentences on specific gaps. Be precise, no hand-waving.",
            },
            "positioning_notes": {
                "type": "string",
                "description": "1-3 sentences on genuine strengths to emphasize for this role.",
            },
            "rationale": {
                "type": "string",
                "description": "2-3 sentences explaining the tier assignment.",
            },
        },
        "required": [
            "tier", "skill_fit", "preference_alignment", "recommended_resume_version",
            "gap_assessment", "positioning_notes", "rationale",
        ],
    },
}

# Kept for backwards-compat with tests
SCORING_SCHEMA = {
    "type": "object",
    "properties": {k: v for k, v in SCORE_TOOL["input_schema"]["properties"].items()},
    "required": SCORE_TOOL["input_schema"]["required"],
}


def _format_signals(signals: dict) -> str:
    lines = []
    for level, items in signals.items():
        if items:
            lines.append(f"{level.upper()}:")
            for item in items:
                lines.append(f"  - {item}")
    return "\n".join(lines)


def _format_tier_defs(tier_defs: dict) -> str:
    return "\n".join(f"  {tier}: {desc}" for tier, desc in tier_defs.items())


def build_scoring_messages(job: Job, profile: dict, narrative_guidance: str) -> list[dict]:
    """
    Returns an Anthropic messages list with cache_control on the expensive static blocks.
    The profile + narrative are cached; the per-job description is not.
    """
    scoring_cfg = profile.get("scoring", {})
    skill_signals = scoring_cfg.get("skill_fit_signals", {})
    pref_signals = scoring_cfg.get("preference_alignment_signals", {})
    tier_defs = scoring_cfg.get("tier_definitions", {})
    honesty_notes = scoring_cfg.get("honesty_calibration", [])
    not_targeting = profile.get("not_targeting", [])

    system_block = f"""You are evaluating job postings against a candidate profile to assign a fit tier.
You will call the record_score tool with your assessment.

HONESTY RULES — these override everything else:
{chr(10).join(f"- {n}" for n in honesty_notes)}

NOT TARGETING (automatic Skip if primary function matches):
{chr(10).join(f"- {t}" for t in not_targeting)}

TIER DEFINITIONS:
{_format_tier_defs(tier_defs)}

SKILL FIT SIGNALS:
{_format_signals(skill_signals)}

PREFERENCE ALIGNMENT SIGNALS:
{_format_signals(pref_signals)}"""

    narrative_block = f"""## Candidate narrative and positioning strategy

{narrative_guidance}"""

    jd_chars = settings.truncation(profile, "score_jd_chars")
    job_block = f"""## Job posting to evaluate

Company: {job.company}
Title: {job.title}
Location: {job.location}

Description:
{job.description_raw[:jd_chars]}

Call record_score with your assessment."""

    return [
        {
            "role": "user",
            "content": [
                # Static system context — cache this
                {
                    "type": "text",
                    "text": system_block,
                    "cache_control": {"type": "ephemeral"},
                },
                # Narrative — also static per session, cache it
                {
                    "type": "text",
                    "text": narrative_block,
                    "cache_control": {"type": "ephemeral"},
                },
                # Per-job content — not cached
                {
                    "type": "text",
                    "text": job_block,
                },
            ],
        }
    ]
