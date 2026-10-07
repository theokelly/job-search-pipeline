"""Shared pipeline plumbing: echo, score replication, profile path constants.

Both the CLI (`cli.py`) and the Streamlit UI import the stage entry points
through `pipeline.py`, which re-exports from `stages/`. This module holds the
small shared pieces so the stage modules don't import each other.
"""
import sys
from typing import Callable

from ..db import (
    init_db, get_session, upsert_job, get_or_create_meta,
    Job, Score, Application, User, UserJobMeta,
)
from .. import settings
from .. import clustering


PROFILE_PATH_DEFAULT = "profile/theo.yaml"
NARRATIVE_PATH = "profile/narrative_guidance.md"

# Default echo writes to stdout. Streamlit / tests pass their own echo callable.
# Kept dependency-free so this module loads in the streamlit Python (no typer there).
def _default_echo(msg: str, err: bool = False) -> None:
    stream = sys.stderr if err else sys.stdout
    print(msg, file=stream)


def _replicate_score(original: Score, target_job_id: str) -> Score:
    """Return a new Score row pointing at target_job_id, with all other fields copied."""
    return Score(
        job_id=target_job_id,
        user_id=original.user_id,
        tier=original.tier,
        skill_fit=original.skill_fit,
        preference_alignment=original.preference_alignment,
        recommended_resume_version=original.recommended_resume_version,
        gap_assessment=original.gap_assessment,
        positioning_notes=original.positioning_notes,
        rationale=original.rationale,
        raw_response=original.raw_response,
        scored_at=original.scored_at,
    )
