"""
Anthropic Message Batches helpers — 50% discount on input + output tokens.

Two flows:
1. Inline (default): submit a batch, poll up to `batches.poll_timeout_seconds`,
   parse results. Caller blocks until done OR the timeout expires.
2. Detached: when the inline poll times out, the batch_id is returned to the
   caller, which prints it. The user runs `job-agent batch-fetch <id>` later to
   pick up results.

Both prescreen and scoring use the same scaffolding; only the request builder
and the result handler differ.
"""
import os
import time
from typing import Callable, Iterable

import anthropic

from . import settings
from .db import Job, Score
from .scoring.prompt import build_scoring_messages, SCORE_TOOL
from .scoring.prescreen import build_prescreen_prompt, parse_prescreen_response
from .scoring.score import _score_from_tool_input


# ---------------------------------------------------------------------------
# Batch submission
# ---------------------------------------------------------------------------

def _client() -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])


def submit_prescreen_batch(jobs: list[Job], profile: dict) -> tuple[str, dict[str, Job]]:
    """Submit a prescreen batch. Returns (batch_id, jobs_by_custom_id)."""
    model = settings.model_for(profile, "prescreen")
    client = _client()
    requests = []
    jobs_by_id: dict[str, Job] = {}
    for job in jobs:
        custom_id = f"prescreen_{job.id}"
        jobs_by_id[custom_id] = job
        requests.append({
            "custom_id": custom_id,
            "params": {
                "model": model,
                "max_tokens": 60,
                "messages": [{"role": "user", "content": build_prescreen_prompt(job, profile)}],
            },
        })
    batch = client.messages.batches.create(requests=requests)
    return batch.id, jobs_by_id


def submit_score_batch(jobs: list[Job], profile: dict, narrative_guidance: str) -> tuple[str, dict[str, Job]]:
    """Submit a scoring batch. Returns (batch_id, jobs_by_custom_id)."""
    model = settings.model_for(profile, "score")
    client = _client()
    requests = []
    jobs_by_id: dict[str, Job] = {}
    for job in jobs:
        custom_id = f"score_{job.id}"
        jobs_by_id[custom_id] = job
        requests.append({
            "custom_id": custom_id,
            "params": {
                "model": model,
                "max_tokens": 1024,
                "tools": [SCORE_TOOL],
                "tool_choice": {"type": "tool", "name": "record_score"},
                "messages": build_scoring_messages(job, profile, narrative_guidance),
            },
        })
    batch = client.messages.batches.create(requests=requests)
    return batch.id, jobs_by_id


# ---------------------------------------------------------------------------
# Polling + result fetch
# ---------------------------------------------------------------------------

class BatchTimeout(Exception):
    """Raised when a batch doesn't complete within the configured poll window.

    Callers should catch this and surface the batch_id to the user (so they can
    run `job-agent batch-fetch <id>` later)."""
    def __init__(self, batch_id: str):
        super().__init__(f"Batch {batch_id} did not finish within timeout")
        self.batch_id = batch_id


def poll_until_ended(batch_id: str, profile: dict, on_tick: Callable[[str], None] | None = None) -> None:
    """Block until the batch is `ended`. Raises BatchTimeout otherwise."""
    cfg = settings.batches_config(profile)
    timeout = float(cfg["poll_timeout_seconds"])
    interval = float(cfg["poll_interval_seconds"])
    client = _client()
    deadline = time.monotonic() + timeout
    while True:
        batch = client.messages.batches.retrieve(batch_id)
        status = getattr(batch, "processing_status", None)
        if on_tick:
            counts = getattr(batch, "request_counts", None)
            done = (getattr(counts, "succeeded", 0) + getattr(counts, "errored", 0)
                    + getattr(counts, "canceled", 0) + getattr(counts, "expired", 0)) if counts else 0
            total = (done + getattr(counts, "processing", 0)) if counts else 0
            on_tick(f"{status} ({done}/{total})")
        if status == "ended":
            return
        if time.monotonic() >= deadline:
            raise BatchTimeout(batch_id)
        time.sleep(interval)


def iter_results(batch_id: str) -> Iterable:
    """Stream `(custom_id, result)` from a finished batch."""
    client = _client()
    for entry in client.messages.batches.results(batch_id):
        yield entry


# ---------------------------------------------------------------------------
# Result handlers — turn raw batch results into typed records
# ---------------------------------------------------------------------------

def collect_prescreen_results(batch_id: str, jobs_by_id: dict[str, Job]) -> list[tuple[Job, bool, str]]:
    """Returns [(job, approved, reason), ...]."""
    out: list[tuple[Job, bool, str]] = []
    for entry in iter_results(batch_id):
        custom_id = entry.custom_id
        job = jobs_by_id.get(custom_id)
        if job is None:
            continue
        # The result envelope has .result.type = 'succeeded' | 'errored' | etc.
        result = entry.result
        if result.type != "succeeded":
            out.append((job, False, f"REJECT - batch error: {result.type}"))
            continue
        message = result.message
        text = ""
        for block in message.content:
            if getattr(block, "type", None) == "text":
                text = block.text
                break
        approved, reason = parse_prescreen_response(text)
        out.append((job, approved, reason))
    return out


def collect_score_results(batch_id: str, jobs_by_id: dict[str, Job], user_id: int) -> list[tuple[Job, Score]]:
    """Returns [(job, Score), ...]. Errored entries are dropped (logged elsewhere)."""
    out: list[tuple[Job, Score]] = []
    for entry in iter_results(batch_id):
        custom_id = entry.custom_id
        job = jobs_by_id.get(custom_id)
        if job is None:
            continue
        result = entry.result
        if result.type != "succeeded":
            continue
        message = result.message
        tool_use = None
        for block in message.content:
            if getattr(block, "type", None) == "tool_use":
                tool_use = block
                break
        if tool_use is None:
            continue
        score = _score_from_tool_input(job, user_id, tool_use.input)
        out.append((job, score))
    return out
