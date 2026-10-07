# Generalization Plan: Publishing to GitHub

Context file for a future Claude Code session. Written 2026-07-17 after the API-cost
review (see git-less workspace state at that date; cost changes already applied).
Goal: make **this** workspace the publishable repo and retire the stale public fork.

---

## Current state (as of 2026-07-17)

- `/workspace` is **not** a git repository. It is the live, current codebase.
- `/workspace/job-application-pipeline-public/` is a **stale fork** (snapshot ~2026-05-22,
  single commit, remote `git@github.com:theokelly/job-application-pipeline.git`).
  It is missing `batches.py`, `output.py`, and the LinkedIn/email/Greenhouse discovery
  modules; `cli.py` / `pipeline.py` / `clustering.py` / `config.py` have diverged.
  **Do not merge code from it** — only harvest its packaging assets:
  - `.gitignore` (verified good: excludes `profile/`, `.env`, `data/`, `outputs/`,
    `HANDOFF.md`, calibration test + fixtures)
  - `profile_template/` (incl. `example.yaml`)
  - `LICENSE`
  - Its git history is clean — only `.env.example` was ever committed. But start a
    **fresh** repo here rather than reusing that history; simpler and zero leak risk.
- The cost-review changes (2026-07-17) are already in place: Strong-Fit-only generation
  default, expanded title filters/prescreen signals in `theo.yaml`, resume-glob fix in
  `config.py`, etc. Don't redo them.

## Blocking check before publishing

Sensitive data lives on disk in this workspace (fine locally, must never be committed):
`profile/` (real CV, phone, email), `.env`, `data/*.sqlite*`, `outputs/`, `HANDOFF.md`.
After `git init` + copying the fork's `.gitignore`, run `git status` and verify NONE of
these appear as untracked-to-be-added. Also grep the tracked set for personal strings
(`theokelly`, `theomkelly`, phone `203 858`, "Theo Kelly") before the first commit.

---

## Step-by-step plan

### 1. Repo bootstrap
- `git init` in `/workspace`; copy `.gitignore`, `LICENSE`, `profile_template/` from
  the public fork. Delete nothing yet.
- Confirm `.dockerignore` similarly excludes profile/data/outputs.
- First commit = code + template only. Run the sensitive-string grep first (above).

### 2. De-Theo the code (specific known spots)
- `src/job_agent/pipeline.py`: `PROFILE_PATH_DEFAULT = "profile/theo.yaml"` and
  `NARRATIVE_PATH = "profile/narrative_guidance.md"` — derive from a single
  `JOB_AGENT_PROFILE` env var / config; narrative path should be derived from the
  profile file's directory, not a module constant.
- `src/job_agent/cli.py`: `--user` defaults to `"theo"` in `score`, `generate`, `run`;
  `run` also hardcodes `load_profile("profile/theo.yaml")` (line ~124). Default the
  user from env or the profile yaml's `user:` block.
- `pipeline.py` `generate_one`: falls back to `"theo"` slug if user lookup fails
  (fixed to look up the real slug 2026-07-17, but the fallback literal remains).
- `src/job_agent/settings.py`: `_FALLBACK_EU_TERMS`, `_FALLBACK_NON_EU`,
  `_FALLBACK_REMOTE_SIGNALS` encode Theo's EU-relocation geography as built-in
  defaults. Move canonical lists into `profile_template/example.yaml`; keep only a
  minimal neutral fallback (or fail loudly if the profile lacks a `geography:` block).
- `src/job_agent/clustering.py`: `_NL_TERMS` / `_UK_TERMS` and the NL > UK > remote-EU
  priority ordering in `geo_priority` are Theo's preferences. Make the priority list
  profile-driven (e.g. `geography.location_priority: [netherlands-terms, uk-terms, ...]`).
- `app/streamlit_app.py` + `run_ui.sh`: check for hardcoded "theo" / profile paths.
- Prompts in `scoring/prescreen.py` template from yaml already (good); verify
  `scoring/prompt.py` and `generation/*.py` have no hardcoded personal facts.

### 3. Docs
- Rewrite `README.md` around the `profile_template/` onboarding path (copy template →
  fill in → init-user → run). Current README documents Theo's daily usage.
- Regenerate the cost-reference table after a few post-optimization runs (the numbers
  changed substantially on 2026-07-17; current table is estimates).
- Keep `HANDOFF.md` gitignored (personal planning). Consider a public `ARCHITECTURE.md`
  distilled from it (scrub personal details).

### 4. Hygiene
- GitHub Action: run the two offline test suites (`tests/test_pre_filter.py`,
  `tests/test_scoring_contract.py`). Note: `test_scoring_calibration.py` and
  `tests/fixtures/` are gitignored (real postings + profile) — CI must not need them.
- Pin dependencies (`uv lock` or pinned `pyproject.toml`).
- `pip install pytest` was needed manually in the container on 2026-07-17 — check the
  dev dependency group / entrypoint installs it.

### 5. Retire the fork
- Once this repo is pushed (new GitHub repo or force-replace the existing
  `job-application-pipeline` remote), delete `/workspace/job-application-pipeline-public/`
  locally. It contains a real `.env`, `profile/theo.yaml`, and `data/*.sqlite` on disk.

---

## Deferred technical debt (fold in during this pass)

- **`datetime.utcnow()` sweep** (deprecated in 3.12): ~22 call sites across `src/` and
  `app/`. CAUTION: the SQLite DB stores naive datetimes; a blind switch to aware
  `datetime.now(UTC)` breaks comparisons against existing rows (`last_seen < cutoff`).
  Either migrate stored values or use `datetime.now(UTC).replace(tzinfo=None)`.
- **Batch the generation step** through `batches.py` scaffolding if bulk WR generation
  is ever re-enabled (50% discount); low value at Strong-Fit-only volume.
- `score.py` `batch-fetch` path doesn't replicate scores to cluster siblings (echoed
  warning in `_score_batch` timeout handler) — acknowledged debt.
- Title-filter quirk: term `"swe"` substring-matches e.g. "Sweden" in titles.
- Title-filter drops never enter the DB (unrecoverable); prescreen rejects are
  UI-overridable. Rule of thumb: only historically-clean patterns in
  `title_filter_terms`, judgment calls in `prescreen.reject_signals`.

## Verification when done

- Fresh clone + `profile_template` copy runs `discover → score --limit 2 → generate`
  end-to-end with a dummy profile and a test API key.
- `git grep -iE "theo|203.?858|theomkelly"` over tracked files returns nothing.
- Offline tests green in CI.
