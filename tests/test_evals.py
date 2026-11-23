"""Unit tests for the eval harness comparison logic (no API calls)."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace

_spec = importlib.util.spec_from_file_location(
    "run_evals", Path(__file__).resolve().parent.parent / "evals" / "run_evals.py")
run_evals = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(run_evals)


def _score(tier, skill, pref):
    return SimpleNamespace(tier=tier, skill_fit=skill, preference_alignment=pref)


def test_verdict_pass():
    assert run_evals.verdict_pass("approved", True)
    assert run_evals.verdict_pass("rejected", False)
    assert not run_evals.verdict_pass("approved", False)
    assert not run_evals.verdict_pass("rejected", True)


def test_score_pass_exact_and_within_band():
    assert run_evals.score_pass("Strong Fit", 82, 95, _score("Strong Fit", 82, 95))
    assert run_evals.score_pass("Strong Fit", 82, 95, _score("Strong Fit", 70, 85))
    assert run_evals.score_pass("Strong Fit", 82, 95, _score("Strong Fit", 97, 80))


def test_score_pass_outside_band_and_wrong_tier():
    assert not run_evals.score_pass("Strong Fit", 82, 95, _score("Strong Fit", 60, 95))
    assert not run_evals.score_pass("Strong Fit", 82, 95, _score("Strong Fit", 82, 70))
    assert not run_evals.score_pass("Strong Fit", 82, 95, _score("Worth Reviewing", 82, 95))


def test_score_pass_without_references_checks_tier_only():
    assert run_evals.score_pass("Worth Reviewing", None, None, _score("Worth Reviewing", 5, 5))
    assert not run_evals.score_pass("Worth Reviewing", None, None, _score("Skip", 90, 90))
