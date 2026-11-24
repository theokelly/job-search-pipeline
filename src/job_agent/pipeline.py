"""
Pipeline orchestration — thin re-export shim.

The stage implementations live in `stages/`:
    stages/discover.py      run_discover
    stages/housekeeping.py  run_sweep, run_prune_closed
    stages/score.py         run_score
    stages/score_batch.py   fetch_score_batch, fetch_prescreen_batch
    stages/generate.py      run_generate
    stages/single.py        score_one, generate_one, mark_posting_status
    stages/ingest.py        run_ingest

Both the CLI (`cli.py`) and the Streamlit UI import from this module, so it
re-exports every public name to keep those import sites unchanged. Private
helpers are re-exported too — tests import `_plan_scoring` from here.
"""
from .stages._common import PROFILE_PATH_DEFAULT, NARRATIVE_PATH, _default_echo, _replicate_score
from .stages.discover import run_discover
from .stages.housekeeping import run_sweep, run_prune_closed, _RateLimiter
from .stages.score import run_score
from .stages.planning import _plan_scoring
from .stages.prescreen_filter import _prescreen_filter
from .stages.score_batch import _score_batch, fetch_score_batch, fetch_prescreen_batch
from .stages.generate import run_generate
from .stages.single import score_one, generate_one, mark_posting_status
from .stages.ingest import run_ingest, INGESTED, ALREADY_PRESENT, FETCH_FAILED

__all__ = [
    "PROFILE_PATH_DEFAULT",
    "NARRATIVE_PATH",
    "run_discover",
    "run_sweep",
    "run_prune_closed",
    "run_score",
    "fetch_score_batch",
    "fetch_prescreen_batch",
    "run_generate",
    "score_one",
    "generate_one",
    "mark_posting_status",
    "run_ingest",
    "INGESTED",
    "ALREADY_PRESENT",
    "FETCH_FAILED",
    # Private helpers, re-exported deliberately: tests and the eval
    # harness import them from here (see docstring above).
    "_default_echo",
    "_replicate_score",
    "_RateLimiter",
    "_plan_scoring",
    "_prescreen_filter",
    "_score_batch",
]
