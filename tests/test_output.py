"""Characterization tests for job_agent.output (Phase C).

The application-packet writers: HTML-to-plain normalization, slugify,
the per-job markdown card, and write_outputs' file layout.
"""
from job_agent.db import Job, Score
from job_agent import output


def _job(source="greenhouse_api"):
    return Job(id="j1", company="Acme BV", title="Head of AI & Data!",
               location="Amsterdam", url="https://example.com/j1",
               source=source, description_raw="JD text")


def _score():
    return Score(job_id="j1", user_id=1, tier="Strong Fit", skill_fit=82,
                 preference_alignment=90, gap_assessment="gaps",
                 positioning_notes="notes", rationale="why")


def test_plain_strips_tags_and_entities():
    assert output._plain("<p>Hello <b>world</b></p>") == "Hello world"
    assert output._plain("&lt;h2&gt;Encoded&lt;/h2&gt;") == "Encoded"
    assert output._plain("Fish &amp; Chips") == "Fish & Chips"


def test_slugify():
    assert output.slugify("Head of AI & Data!") == "head-of-ai-data"
    assert output.slugify("  Spaces  Everywhere  ") == "spaces-everywhere"
    assert len(output.slugify("x" * 100)) == 40


def test_job_card_contents():
    card = output._job_card(_job(), _score(), "2026-11-24")
    assert "Acme BV" in card and "Head of AI & Data!" in card
    assert "Greenhouse ATS" in card  # source label mapping
    assert "[View posting](https://example.com/j1)" in card
    assert "Strong Fit" in card


def test_job_card_unknown_source_and_missing_url():
    job = _job(source="carrier_pigeon")
    job.url = ""
    card = output._job_card(job, _score(), "2026-11-24")
    assert "carrier_pigeon" in card  # unknown sources pass through raw
    assert "_URL not available_" in card


def test_write_outputs_layout(tmp_path):
    out_dir = output.write_outputs(
        _job(), _score(), "COVER LETTER", "RESUME REC",
        base_dir=str(tmp_path),
    )
    # Folder: <date>_<company-slug>_<title-slug> under base_dir/<user_slug>.
    assert out_dir.parent == tmp_path / "theo"
    assert out_dir.name.endswith("_acme-bv_head-of-ai-data")
    names = sorted(p.name for p in out_dir.iterdir())
    assert names == ["cover_letter.md", "job.md", "resume_recommendation.md", "score.json"]
    assert (out_dir / "cover_letter.md").read_text() == "COVER LETTER"
    assert (out_dir / "resume_recommendation.md").read_text() == "RESUME REC"
    import json
    score = json.loads((out_dir / "score.json").read_text())
    assert score["tier"] == "Strong Fit" and score["company"] == "Acme BV"


def test_write_outputs_optional_files(tmp_path):
    out_dir = output.write_outputs(
        _job(), _score(), "CL", "RR",
        application_answers="ANSWERS", grounding_check="GROUNDING",
        base_dir=str(tmp_path),
    )
    names = sorted(p.name for p in out_dir.iterdir())
    assert "answers.md" in names and "grounding_check.md" in names
