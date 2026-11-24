"""Fit-tier names — the single source of truth.

Tier strings are persisted in the DB (Score.tier), produced by the
scoring prompt's tool schema, and matched by the CLI, the generation
stage, and the UI. They used to be bare literals in six files, where
a drift in any one would fail silently. Import from here.
"""

STRONG_FIT = "Strong Fit"
WORTH_REVIEWING = "Worth Reviewing"
LONG_SHOT = "Long Shot"
SKIP = "Skip"

ALL_TIERS = (STRONG_FIT, WORTH_REVIEWING, LONG_SHOT, SKIP)
