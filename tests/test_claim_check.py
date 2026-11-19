"""Tests for the deterministic claim check (generation/claim_check.py).

The Philips regressions: a source span "13-user pilot across 4 functions"
must pass verbatim use and block the model's upgrades — "piloted 4 use
cases" (unit swap) and "100% adoption" (invented percentage).
"""
from job_agent.generation.claim_check import (
    build_corpus,
    check_edit,
    check_suggested,
)

SOURCES = [
    "Led the AI adoption pilot: a 13-user pilot across 4 functions, gated before scale.",
    "Managed an analytics team of 6 and a $60M portfolio in 2024.",
]


def _corpus():
    return build_corpus(SOURCES)


def test_verbatim_numbers_pass():
    assert check_suggested("Ran a 13-user pilot across 4 functions", _corpus()) == []


def test_unit_swap_blocked():
    flags = check_suggested("Piloted 4 use cases with stakeholders", _corpus())
    assert flags
    assert any("'4'" in f for f in flags)


def test_invented_percentage_blocked():
    flags = check_suggested("Achieved 100% adoption without IT bottlenecks", _corpus())
    assert any("100%" in f for f in flags)


def test_value_contradiction_flagged():
    flags = check_suggested(
        "Managed an analytics team of 12",
        _corpus(),
        original="Managed an analytics team of 6",
    )
    assert any("changed from 6 to 12" in f for f in flags)


def test_years_are_context_free_but_must_exist():
    assert check_suggested("Grew the portfolio in 2024", _corpus()) == []
    assert check_suggested("Grew the portfolio in 2019", _corpus())


def test_currency_format_variants_match():
    assert check_suggested("Owned a $60 million portfolio", _corpus()) == []


def test_empty_corpus_fails_open():
    assert check_suggested("Achieved 100% adoption", build_corpus([])) == []


def test_reorder_never_checked():
    edit = {"operation": "reorder", "original": "Bullet 9", "suggested": "__FIRST__"}
    assert check_edit(edit, _corpus(), require_quote=True) == []


def test_insert_requires_grounded_source_quote():
    base = {"operation": "replace", "section": "S", "original": "<UNKNOWN>",
            "suggested": "Ran a 13-user pilot across 4 functions", "reason": "x"}
    flags = check_edit(base, _corpus(), require_quote=True)
    assert any("source_quote" in f for f in flags)

    ok = {**base, "source_quote": "13-user pilot across 4 functions"}
    assert check_edit(ok, _corpus(), require_quote=True) == []

    fabricated = {**base, "source_quote": "Led a 500-person transformation"}
    assert check_edit(fabricated, _corpus(), require_quote=True)
