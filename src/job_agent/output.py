import json
import re
import html as _html
from datetime import datetime, timezone
from pathlib import Path
from .db import Job, Score

_SOURCE_LABELS = {
    "greenhouse": "Greenhouse ATS",
    "greenhouse_api": "Greenhouse ATS",
    "lever": "Lever ATS",
    "lever_api": "Lever ATS",
    "ashby_api": "Ashby ATS",
    "workday_api": "Workday",
    "linkedin": "LinkedIn (email alert)",
    "linkedin_search": "LinkedIn (direct search)",
    "indeed": "Indeed (email alert)",
    "glassdoor": "Glassdoor (email alert)",
    "email": "Email alert",
    "manual": "Manual entry",
}

_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _plain(text: str) -> str:
    """Strip HTML tags and decode entities. Handles both raw and entity-encoded HTML."""
    text = _html.unescape(text)       # &lt;h2&gt; → <h2>
    text = _HTML_TAG_RE.sub(" ", text) # <h2> → space
    text = _html.unescape(text)       # decode any remaining entities (e.g. &amp; → &)
    return re.sub(r"\s{2,}", " ", text).strip()


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40]


def _job_card(job: Job, score: Score, generated_at: str) -> str:
    source_label = _SOURCE_LABELS.get(job.source, job.source)
    url_line = f"[View posting]({job.url})" if job.url else "_URL not available_"
    first_seen = job.first_seen.strftime("%Y-%m-%d") if job.first_seen else "unknown"

    jd_preview = _plain(job.description_raw)
    if jd_preview:
        words = jd_preview.split()
        if len(words) > 400:
            jd_preview = " ".join(words[:400]) + " …"
    else:
        jd_preview = "_No description available_"

    return f"""# {job.title}
**{job.company}** — {job.location}

{url_line}

| Field | Value |
|---|---|
| Source | {source_label} |
| First seen | {first_seen} |
| Materials generated | {generated_at} |
| Tier | {score.tier} |
| Skill fit | {score.skill_fit}/100 |
| Preference alignment | {score.preference_alignment}/100 |
| Resume version | {score.recommended_resume_version} |

## Score summary
{score.rationale}

**Gaps:** {score.gap_assessment}

## Job description
{jd_preview}
"""


def write_outputs(
    job: Job,
    score: Score,
    cover_letter: str,
    resume_recommendation: str,
    application_answers: str = "",
    grounding_check: str = "",
    user_slug: str = "theo",
    base_dir: str = "outputs",
) -> Path:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    date_str = now.strftime("%Y-%m-%d")
    folder_name = f"{date_str}_{slugify(job.company)}_{slugify(job.title)}"
    out_dir = Path(base_dir) / user_slug / folder_name
    out_dir.mkdir(parents=True, exist_ok=True)

    (out_dir / "job.md").write_text(_job_card(job, score, generated_at=date_str))
    (out_dir / "cover_letter.md").write_text(cover_letter)
    (out_dir / "resume_recommendation.md").write_text(resume_recommendation)
    if application_answers:
        (out_dir / "answers.md").write_text(application_answers)
    if grounding_check:
        (out_dir / "grounding_check.md").write_text(grounding_check)
    (out_dir / "score.json").write_text(json.dumps({
        "tier": score.tier,
        "skill_fit": score.skill_fit,
        "preference_alignment": score.preference_alignment,
        "recommended_resume_version": score.recommended_resume_version,
        "gap_assessment": score.gap_assessment,
        "positioning_notes": score.positioning_notes,
        "rationale": score.rationale,
        "job_url": job.url,
        "source": job.source,
        "company": job.company,
        "title": job.title,
        "location": job.location,
        "first_seen": job.first_seen.isoformat() if job.first_seen else None,
        "generated_at": now.isoformat(),
    }, indent=2))

    return out_dir
