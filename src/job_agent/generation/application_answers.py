"""Pre-drafted answers to the standard questions on job application forms.

Every application asks the same handful of questions (work authorization,
salary, notice period, location...). This module drafts them once per
application from the profile so Theo can copy-paste instead of retyping.
Anything the profile can't answer is marked TODO for him to fill in.

Runs on Haiku — costs a fraction of a cent per application.
"""
import os
import anthropic
from ..db import Job
from .. import settings


# Questions that appear on nearly every application form. Keys map to the
# `application_answers` block in theo.yaml (all optional — missing ones
# become TODO markers in the output).
STANDARD_QUESTIONS = [
    ("work_authorization", "Are you authorized to work in the country of this role?"),
    ("visa_sponsorship", "Do you require visa sponsorship, now or in the future?"),
    ("location", "Where are you currently located?"),
    ("willing_to_relocate", "Are you willing to relocate?"),
    ("salary_expectation", "What are your salary expectations?"),
    ("notice_period", "What is your notice period?"),
    ("earliest_start_date", "What is your earliest available start date?"),
    ("languages", "What languages do you speak?"),
]


def _known_answers(profile: dict) -> dict:
    """Merge profile facts with the user's vetted application_answers block."""
    answers = dict(profile.get("application_answers") or {})
    # Facts the pipeline already knows; yaml values take precedence.
    defaults = {
        "location": "Amsterdam, Netherlands",
        "work_authorization": (
            "EU citizen (Greece) — authorized to work anywhere in the EU/EEA, "
            "no sponsorship required. Also a US citizen."
        ),
        "visa_sponsorship": "No — EU citizen, no sponsorship required.",
        "willing_to_relocate": "Already based in Amsterdam.",
    }
    for key, value in defaults.items():
        answers.setdefault(key, value)
    return answers


def build_application_answers_messages(job: Job, profile: dict, resume_text: str) -> list[dict]:
    answers = _known_answers(profile)
    answered = "\n".join(f"- {key}: {value}" for key, value in answers.items() if value)
    missing = [key for key, _ in STANDARD_QUESTIONS if not answers.get(key)]
    missing_note = (
        f"\nThe profile does NOT provide answers for: {', '.join(missing)}. "
        f"Mark these TODO." if missing else ""
    )
    questions = "\n".join(f"- {q}" for _, q in STANDARD_QUESTIONS)
    jd_chars = settings.truncation(profile, "answers_jd_chars")
    resume_chars = settings.truncation(profile, "answers_resume_chars")

    user_text = f"""## Target role
Company: {job.company}
Title: {job.title}
Location: {job.location}

Job description (may include screening questions — extract any the posting asks explicitly):
{job.description_raw[:jd_chars]}

## Known facts about the candidate (use verbatim where they answer a question)
{answered}
{missing_note}

## Resume (for job-specific experience questions)
{resume_text[:resume_chars]}

## Task
Write an `answers.md` file the candidate can copy-paste from when filling out this application's form.

Format:
```markdown
# Application answers — {job.company}, {job.title}

## Standard questions
- **<question>**\n  <answer>
... (one entry per question below)

## Job-specific questions
- **<question extracted from the posting>**\n  <answer>
(omit this section if the posting asks none)
```

Standard questions to cover:
{questions}

Rules:
- Answer ONLY from the known facts and resume. Never invent salary figures, dates, or language proficiency.
- Anything you cannot answer: write `**TODO — fill in before submitting**` as the answer. If a `*_guidance` fact above is relevant to a TODO answer, quote it in the TODO line so the candidate can decide (e.g. salary range guidance).
- Keep each answer to 1-2 lines, plain text, no preamble. Return only the markdown file content."""
    return [{"role": "user", "content": user_text}]


def generate_application_answers(job: Job, profile: dict, resume_text: str) -> str:
    """Draft application-form answers. Model from `models.application_answers`
    (Haiku by default)."""
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    model = settings.model_for(profile, "application_answers")
    messages = build_application_answers_messages(job, profile, resume_text)
    response = client.messages.create(
        model=model,
        max_tokens=1024,
        messages=messages,
    )
    return response.content[0].text
