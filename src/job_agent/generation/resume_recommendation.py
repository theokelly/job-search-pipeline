import os
import anthropic
from ..db import Job, Score
from .. import settings

RESUME_TOOL = {
    "name": "record_resume_recommendation",
    "description": "Record the resume tailoring recommendation for this application.",
    "input_schema": {
        "type": "object",
        "properties": {
            "version_rationale": {
                "type": "string",
                "description": "One sentence: which resume version and why.",
            },
            "edits": {
                "type": "array",
                "description": "1-3 specific text-level edits. Each edit is surgical and concrete.",
                "items": {
                    "type": "object",
                    "properties": {
                        "section": {"type": "string", "description": "Which section or bullet to edit."},
                        "original": {"type": "string", "description": "The existing text to change."},
                        "suggested": {"type": "string", "description": "The replacement text."},
                        "reason": {"type": "string", "description": "Why this edit helps for this role."},
                    },
                    "required": ["section", "suggested", "reason"],
                },
                "minItems": 1,
                "maxItems": 3,
            },
        },
        "required": ["version_rationale", "edits"],
    },
}


def build_resume_rec_messages(job: Job, score: Score, resume_text: str, profile: dict) -> list[dict]:
    """Render the resume-recommendation messages. Shared by sync + batch paths."""
    resume_chars = settings.truncation(profile, "resume_rec_resume_chars")
    jd_chars = settings.truncation(profile, "resume_rec_jd_chars")
    return [
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": f"""You are advising on how to tailor a resume for a specific job application.
Provide surgical, specific recommendations — not generic advice.

## Resume ({score.recommended_resume_version} version)
{resume_text[:resume_chars]}""",
                    "cache_control": {"type": "ephemeral"},
                },
                {
                    "type": "text",
                    "text": f"""## Target role
Company: {job.company}
Title: {job.title}

Job description:
{job.description_raw[:jd_chars]}

## Context
Positioning strengths: {score.positioning_notes}
Gaps: {score.gap_assessment}

Call record_resume_recommendation with 1-3 specific text-level edits.
Do not suggest rewriting the whole resume. Focus on the intro paragraph, 1-2 bullets, or keyword additions that matter most for this role.""",
                },
            ],
        }
    ]


def _bullet_lines(label: str, value: str) -> list[str]:
    """Render a (possibly multi-paragraph) bullet item.

    Returns a list of lines forming one Markdown list item with continuation
    indentation. Paragraph breaks (blank lines) within the value are preserved.
    Italics aren't used: they can't span newlines in CommonMark, so any source
    text with paragraph breaks would break out of italic *and* list mode.
    """
    if value is None or value == "":
        return []
    value = value.strip().replace("\r\n", "\n").replace("\r", "\n")
    parts = value.split("\n")
    out: list[str] = []
    for i, line in enumerate(parts):
        if i == 0:
            out.append(f"- **{label}:** {line}")
        elif line == "":
            # Blank line → paragraph break within the list item. Stays empty so
            # Markdown renders the gap; the next non-empty line continues indented.
            out.append("")
        else:
            out.append(f"  {line}")
    return out


def format_resume_rec(score: Score, data: dict) -> str:
    """Render the tool-use payload as the markdown that gets written to disk."""
    lines = [f"**Resume version:** {score.recommended_resume_version}",
             f"**Why:** {data['version_rationale']}", ""]
    for edit in data.get("edits", []):
        lines.append(f"**{edit['section']}**")
        lines.append("")
        lines.extend(_bullet_lines("Original", edit.get("original", "")))
        lines.extend(_bullet_lines("Suggested", edit.get("suggested", "")))
        lines.extend(_bullet_lines("Why", edit.get("reason", "")))
        lines.append("")
    return "\n".join(lines)


def generate_resume_recommendation(job: Job, score: Score, resume_text: str, profile: dict) -> str:
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    model = settings.model_for(profile, "resume_recommendation")
    messages = build_resume_rec_messages(job, score, resume_text, profile)

    response = client.messages.create(
        model=model,
        max_tokens=1024,
        tools=[RESUME_TOOL],
        tool_choice={"type": "tool", "name": "record_resume_recommendation"},
        messages=messages,
    )

    tool_use = next(b for b in response.content if b.type == "tool_use")
    return format_resume_rec(score, tool_use.input)
