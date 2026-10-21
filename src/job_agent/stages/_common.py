"""Shared pipeline plumbing: echo, score replication, profile path constants.

Both the CLI (`cli.py`) and the Streamlit UI import the stage entry points
through `pipeline.py`, which re-exports from `stages/`. This module holds the
small shared pieces so the stage modules don't import each other.
"""
import functools
import json
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
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
    print(msg, file=stream, flush=True)


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


# ---------------------------------------------------------------------------
# Structured per-run logging
# ---------------------------------------------------------------------------
# @logged_run("<stage>") wraps the run_* stage entry points: each invocation
# gets a run ID, its wall-clock duration is measured, and a JSON line with
# the stage, timing, and a summary of the return value is appended to
# data/runs.log. Logging never breaks a run — write failures only echo.

RUN_LOG_PATH = "data/runs.log"


def _summarize_result(result) -> dict:
    """Build a JSON-serializable summary from a stage's return value."""
    if isinstance(result, dict):
        return {
            str(k): v for k, v in result.items()
            if isinstance(v, (int, float, str, bool))
        }
    if isinstance(result, int) and not isinstance(result, bool):
        return {"count": result}
    if isinstance(result, (list, tuple)):
        if len(result) == 2 and isinstance(result[0], dict):
            summary = _summarize_result(result[0])
            summary["results"] = len(result[1])
            return summary
        summary: dict = {"count": len(result)}
        tiers: dict[str, int] = {}
        for item in result:
            tier = getattr(item, "tier", None)
            if isinstance(tier, str):
                tiers[tier] = tiers.get(tier, 0) + 1
        if tiers:
            summary["tiers"] = tiers
        return summary
    return {}


def _write_run_log(record: dict) -> None:
    try:
        Path(RUN_LOG_PATH).parent.mkdir(parents=True, exist_ok=True)
        with open(RUN_LOG_PATH, "a") as f:
            f.write(json.dumps(record) + "\n")
    except Exception as e:  # logging must never break a run
        _default_echo(f"[runlog] could not write {RUN_LOG_PATH}: {e}", err=True)


def logged_run(stage: str):
    """Decorate a run_* stage: run ID, timing, and result summary -> runs.log."""
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            echo = kwargs.get("echo") or _default_echo
            run_id = uuid.uuid4().hex[:8]
            started = datetime.now(timezone.utc)
            t0 = time.monotonic()
            echo(f"[{stage}] run {run_id} started")
            try:
                result = fn(*args, **kwargs)
            except Exception as exc:
                _write_run_log({
                    "run_id": run_id,
                    "stage": stage,
                    "started_at": started.isoformat(),
                    "duration_s": round(time.monotonic() - t0, 2),
                    "status": "error",
                    "error": f"{type(exc).__name__}: {exc}",
                })
                raise
            duration = round(time.monotonic() - t0, 2)
            summary = _summarize_result(result)
            _write_run_log({
                "run_id": run_id,
                "stage": stage,
                "started_at": started.isoformat(),
                "duration_s": duration,
                "status": "ok",
                "summary": summary,
            })
            echo(f"[{stage}] run {run_id} done in {duration}s")
            return result
        return wrapper
    return decorator
