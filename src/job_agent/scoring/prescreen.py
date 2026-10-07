"""
Cheap first-pass filter (default: Haiku) that drops obvious mismatches before
the expensive Sonnet scoring call.

The prompt is templated from the profile yaml — persona, geography rules, and
reject signals all live in `theo.yaml` under the `prescreen:` block. To tune the
filter (loosen geography, add reject signals), edit yaml only.
"""
import os
import anthropic
from ..db import Job
from .. import settings


_PROMPT_TEMPLATE = """\
You are a quick pre-filter for a job search pipeline.

{persona}

{geography_rules}

Reject immediately if ANY of the following are true:
{reject_signals_block}

Otherwise approve for full scoring.

Role: {company} — {title} ({location})

Job description excerpt:
{description_preview}

Respond with ONE word (APPROVE or REJECT) followed by a dash and a brief reason (≤8 words).
Example: "APPROVE - analytics leadership role Amsterdam"
Example: "REJECT - requires Dutch language fluency"
"""


def build_prescreen_prompt(job: Job, profile: dict) -> str:
    """Render the prescreen prompt for `job` from the profile yaml."""
    cfg = settings.prescreen_config(profile)
    jd_chars = settings.truncation(profile, "prescreen_jd_chars")

    reject_signals = cfg["reject_signals"]
    reject_block = "\n".join(f"- {s}" for s in reject_signals) if reject_signals else "- (no reject signals configured)"

    return _PROMPT_TEMPLATE.format(
        persona=cfg["persona"] or "(no persona configured)",
        geography_rules=cfg["geography_rules"] or "(no geography rules configured)",
        reject_signals_block=reject_block,
        company=job.company,
        title=job.title,
        location=job.location,
        description_preview=job.description_raw[:jd_chars],
    )


def prescreen_job(job: Job, profile: dict) -> tuple[bool, str]:
    """Return (should_score, reason). Uses the configured prescreen model."""
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    model = settings.model_for(profile, "prescreen")
    prompt = build_prescreen_prompt(job, profile)

    response = client.messages.create(
        model=model,
        max_tokens=60,
        messages=[{"role": "user", "content": prompt}],
    )

    text = response.content[0].text.strip()
    approved = text.upper().startswith("APPROVE")
    return approved, text


def parse_prescreen_response(text: str) -> tuple[bool, str]:
    """Shared parser used by both sync and batch paths."""
    text = (text or "").strip()
    approved = text.upper().startswith("APPROVE")
    return approved, text
