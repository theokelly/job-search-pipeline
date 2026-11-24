# Archived one-off scripts

Migration and one-time repair tools, kept for history. They were written
against a past state of the database/profile and are **not** maintained —
do not run them against the current system without reviewing them first.

- `dedupe_existing.py` — one-time dedupe pass over existing rows from the
  clustering migration.
- `regen_resume_recs.py` — one-time regeneration of resume recommendations
  after a generation-prompt change.

Live tools stay in `scripts/` (`extract_resume.py`,
`build_profile_template.py`, `build_golden_cases.py`, `drive_auth_local.py`).
