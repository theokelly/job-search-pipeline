import os
from datetime import datetime
import anthropic
from ..db import Job, Score
from .. import settings
from .prompt import build_scoring_messages, SCORE_TOOL


def score_job(job: Job, user_id: int, profile: dict, narrative_guidance: str) -> Score:
    """Synchronous (non-batched) scoring call. Used by Streamlit "score now" and
    as the fallback when batches are disabled."""
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    model = settings.model_for(profile, "score")
    messages = build_scoring_messages(job, profile, narrative_guidance)

    response = client.messages.create(
        model=model,
        max_tokens=1024,
        tools=[SCORE_TOOL],
        tool_choice={"type": "tool", "name": "record_score"},
        messages=messages,
    )

    tool_use = next(b for b in response.content if b.type == "tool_use")
    return _score_from_tool_input(job, user_id, tool_use.input)


def _score_from_tool_input(job: Job, user_id: int, data: dict) -> Score:
    """Build a Score row from the tool-use payload. Shared by sync + batch paths."""
    import json
    raw = json.dumps(data)
    return Score(
        job_id=job.id,
        user_id=user_id,
        tier=data["tier"],
        skill_fit=data["skill_fit"],
        preference_alignment=data["preference_alignment"],
        recommended_resume_version=data["recommended_resume_version"],
        gap_assessment=data["gap_assessment"],
        positioning_notes=data["positioning_notes"],
        rationale=data["rationale"],
        raw_response=raw,
        scored_at=datetime.utcnow(),
    )
