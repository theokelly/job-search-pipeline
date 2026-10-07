import os
import anthropic
from ..db import Job, Score
from .. import settings


def _select_examples(examples_text: str, score: Score, limit: int) -> str:
    """Return the full examples block for now. Future: filter by role type."""
    return examples_text[:limit]


def build_cover_letter_messages(job: Job, score: Score, profile_files: dict, profile: dict) -> list[dict]:
    principles = profile_files.get("guidelines/cover_letter_principles.md", "")
    examples_chars = settings.truncation(profile, "cover_letter_examples_chars")
    starls_chars = settings.truncation(profile, "cover_letter_starls_chars")
    meta_chars = settings.truncation(profile, "cover_letter_meta_quotes_chars")
    resume_chars = settings.truncation(profile, "cover_letter_resume_chars")
    jd_chars = settings.truncation(profile, "cover_letter_jd_chars")

    examples = _select_examples(profile_files.get("voice/cover_letter_examples.md", ""), score, examples_chars)
    narrative = profile_files.get("narrative_guidance.md", "")
    starls = profile_files.get("accomplishments/starls.md", "")
    meta_quotes = profile_files.get("accomplishments/meta_performance.md", "")
    elevator_pitch = profile_files.get("voice/elevator_pitch.md", "")
    # Fall back to ai_focused if the recommended version isn't extracted yet —
    # a cover letter must never be generated without any resume content.
    resume_text = (
        profile_files.get(f"resume_versions/{score.recommended_resume_version}.md")
        or profile_files.get("resume_versions/ai_focused.md", "")
    )

    system_text = f"""You are writing a cover letter for a job application. Follow the principles exactly and match the voice of the examples.

## Cover letter principles (MUST follow)
{principles}

## Style and voice examples (few-shot — match this tone and structure exactly)
{examples}"""

    candidate_context = f"""## Candidate narrative
{narrative}

## Elevator pitch
{elevator_pitch}

## STARL accomplishments bank (select 2-3 most relevant)
{starls[:starls_chars]}

## Third-party validation quotes (use at most one, only if it fits naturally)
{meta_quotes[:meta_chars]}

## Resume — {score.recommended_resume_version} version
{resume_text[:resume_chars]}"""

    from datetime import datetime
    today = datetime.utcnow().strftime("%B %Y")

    job_prompt = f"""## Target role
Company: {job.company}
Title: {job.title}
Location: {job.location}
Date for letter header: {today}

Job description:
{job.description_raw[:jd_chars]}

## Scoring context (use to calibrate emphasis — do not reference these notes directly)
Positioning: {score.positioning_notes}
Gaps to avoid over-claiming: {score.gap_assessment}

## Task
Write a complete cover letter. Hard constraints:
- 350-500 words, 4-5 paragraphs
- No em dashes, no "genuinely", no bullet points, no contrast framing as a crutch
- Open with the role + a real reason for applying (NOT "I am writing to express my interest")
- 2 evidence paragraphs with specific numbers and named programs
- Tie-back paragraph connecting evidence to the role's stated needs
- Close with a forward-looking statement; mention being based in Amsterdam (and EU citizenship) in one clause only where location or work authorization is relevant. Never say the candidate is relocating: he already lives in Amsterdam.

Return only the cover letter text. No preamble, no commentary."""

    return [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": system_text, "cache_control": {"type": "ephemeral"}},
                {"type": "text", "text": candidate_context, "cache_control": {"type": "ephemeral"}},
                {"type": "text", "text": job_prompt},
            ],
        }
    ]


def generate_cover_letter(job: Job, score: Score, profile_files: dict, profile: dict) -> str:
    """Generate a cover letter. Model selected per tier from profile yaml
    (`models.cover_letter_by_tier`); falls back to `models.cover_letter_default`."""
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    model = settings.model_for(profile, "cover_letter", tier=score.tier)
    messages = build_cover_letter_messages(job, score, profile_files, profile)

    response = client.messages.create(
        model=model,
        max_tokens=2048,
        messages=messages,
    )

    return response.content[0].text
