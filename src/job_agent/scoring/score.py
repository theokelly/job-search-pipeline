import os
from datetime import datetime, timezone
import anthropic
from ..db import Job, Score
from .. import settings
from .prompt import build_scoring_messages, SCORE_TOOL


# Fields _score_from_tool_input requires. Current scoring models reject
# forced tool_choice, so score_job asks (tool_choice="auto") and validates
# the payload instead of assuming the schema was enforced.
REQUIRED_SCORE_FIELDS = (
    "tier", "skill_fit", "preference_alignment", "recommended_resume_version",
    "gap_assessment", "positioning_notes", "rationale",
)

_MAX_SCORE_ATTEMPTS = 3


def missing_score_fields(data: dict) -> list[str]:
    """Required record_score fields absent from a tool payload."""
    return [k for k in REQUIRED_SCORE_FIELDS if k not in data]


def _with_user_note(messages: list, note: str) -> list:
    """Copy messages with a note appended to the last message's content."""
    out = [dict(m) for m in messages]
    last = out[-1]
    if isinstance(last.get("content"), str):
        last["content"] += "\n\n" + note
    else:
        last["content"] = list(last["content"]) + [{"type": "text", "text": note}]
    return out


def score_job(job: Job, user_id: int, profile: dict, narrative_guidance: str) -> Score:
    """Synchronous (non-batched) scoring call. Used by Streamlit "score now" and
    as the fallback when batches are disabled.

    Asks for the record_score tool (tool_choice="auto"); retries with the
    gaps named when the model answers in text or returns an incomplete
    payload, and raises after the final attempt."""
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    model = settings.model_for(profile, "score")
    messages = build_scoring_messages(job, profile, narrative_guidance)

    missing: list[str] = []
    for attempt in range(_MAX_SCORE_ATTEMPTS):
        attempt_messages = messages
        if attempt:
            notes = ["You must respond by calling the record_score tool."]
            if missing:
                notes.append(
                    "Your previous call missed required fields: "
                    + ", ".join(missing) + ". Include all fields.")
            attempt_messages = _with_user_note(messages, " ".join(notes))
        response = client.messages.create(
            model=model,
            max_tokens=1024,
            tools=[SCORE_TOOL],
            tool_choice={"type": "auto"},
            messages=attempt_messages,
        )
        tool_use = next((b for b in response.content if b.type == "tool_use"), None)
        if tool_use is None:
            missing = []
            continue
        missing = missing_score_fields(tool_use.input)
        if not missing:
            return _score_from_tool_input(job, user_id, tool_use.input)
    raise ValueError(
        f"Scoring model returned no complete record_score call for job {job.id} "
        f"after {_MAX_SCORE_ATTEMPTS} attempts (last missing: {missing})")


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
        scored_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )
