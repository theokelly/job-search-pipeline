"""Grounding check: verify a generated cover letter only claims things the
candidate's materials support.

The cover letter goes out under Theo's name, so invented details are the
worst possible failure mode. This module runs a cheap Haiku pass over the
generated letter against the resume + accomplishments bank and flags any
claim that isn't grounded. Flags are for human review — generation is never
blocked, in keeping with the assistive-not-autonomous design.
"""
import os
import anthropic
from ..db import Job, Score
from .. import settings


def build_grounding_check_messages(
    job: Job,
    score: Score,
    cover_letter: str,
    resume_text: str,
    starls_text: str,
    profile: dict,
) -> list[dict]:
    resume_chars = settings.truncation(profile, "truthfulness_resume_chars")
    user_text = f"""## Role applied for
Company: {job.company}
Title: {job.title}

## Cover letter to verify
{cover_letter}

## Candidate's resume (ground truth for employment history, titles, metrics)
{resume_text[:resume_chars]}

## Accomplishments bank (ground truth for stories and numbers)
{starls_text[:4000]}

## Task
Check every factual claim in the cover letter (job titles, employers, dates,
metrics, program names, skills, degrees) against the resume and accomplishments
bank above.

Return a markdown report in exactly this format:

```markdown
# Grounding check — {job.company}, {job.title}

**Verdict:** PASS | REVIEW

## Flagged claims
(If PASS: write "None — all claims are grounded." If REVIEW: one bullet per
flagged claim, each with the exact quote from the letter and one line on why
it is not supported by the materials.)
```

Rules:
- Reasonable paraphrase is fine ("led analytics" for a bullet saying "managed analytics team of 6").
- Soft claims ("excited about", "drawn to") need no grounding.
- Flag: invented metrics, wrong titles/dates, skills the resume never mentions,
  programs or employers that don't appear in the materials.
- When in doubt, flag it — a false positive costs Theo ten seconds; a false
  negative goes to an employer under his name.
- Return only the markdown report, no preamble."""
    return [{"role": "user", "content": user_text}]


def check_grounding(
    job: Job,
    score: Score,
    cover_letter: str,
    resume_text: str,
    starls_text: str,
    profile: dict,
) -> str:
    """Run the grounding check. Model from `models.truthfulness_check`
    (Haiku by default). Never raises — returns an error report instead."""
    try:
        client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        model = settings.model_for(profile, "truthfulness_check")
        messages = build_grounding_check_messages(job, score, cover_letter, resume_text, starls_text, profile)
        response = client.messages.create(
            model=model,
            max_tokens=1024,
            messages=messages,
        )
        return response.content[0].text
    except Exception as e:
        return (
            f"# Grounding check — {job.company}, {job.title}\n\n"
            f"**Verdict:** REVIEW\n\n## Flagged claims\n"
            f"- Check could not run ({e}). Review the letter manually.\n"
        )
