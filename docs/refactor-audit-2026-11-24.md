# Refactor audit — job-search-pipeline

**Date:** 2026-11-24 · **Snapshot:** main @ `7433826` · **Method:** full read of `src/`, `app/`, `tests/`, docs and diagrams; AST inventory of all 68 Python files (defs, imports, reference counts); targeted greps for duplication, dead code, and literal drift. Read-only — no code was changed.

**Size:** 9,283 LOC (`src/` + `app/`), 179 tests in 22 files, plus the 13-case eval harness.

---

## 1. What the system is (one paragraph per layer)

- **Discovery** (`discovery/`): pulls postings from LinkedIn guest API (`linkedin.py`), ATS APIs (Greenhouse/Lever 15-line shims over `normalizer.py`; Ashby; Workday), Brave-search fan-out across ATS domains (`fanout.py`), Gmail alert emails (`email_alerts.py`), and single-URL extraction (`manual.py` HTML cascade; `fanout.fetch_job_from_url` dispatcher).
- **Funnel stages** (`stages/`): `discover` → rule pre-filter (`filter.py`) → cluster dedupe (`clustering.py`) → Haiku prescreen (`scoring/prescreen.py`) → Sonnet scoring (`scoring/score.py` + `prompt.py`), orchestrated by `stages/score.py` (planning, prescreen filter, batch submit/fetch via `batches.py`) → on-demand generation (`stages/generate.py`, `stages/single.py`) → housekeeping (`sweep`, `prune-closed`) and `ingest`.
- **Generation** (`generation/` + `tailor.py` + `cover_letter_doc.py`): cover letters, resume recommendations, application answers, with the grounding layer (`claim_check.py`, `truthfulness.py`); outputs land in `outputs/` and sync to Drive (`drive_sync.py`, `resume_mirror.py`).
- **Surfaces**: Typer CLI (`cli.py`, 18 commands, lazy imports per command — a deliberate, consistent pattern) and the Streamlit UI (`app/streamlit_app.py`), both over one SQLite DB (`db.py`: models, engine, `upsert_job`, `job_id` — fan-in 22, tight at 162 lines, healthy).
- **Config**: `config.py` loads/validates the profile YAML; `settings.py` is pure accessor functions over the profile dict (models, truncation, batches) with defaults.
- **Ops**: `doctor.py` health checks; cron on muse-vm runs `job-agent run` Mon–Fri 07:30; UI is an always-on container.

## 2. Findings

Severity: **H** = actively risks wrong behavior or hides it · **M** = friction/drift risk · **L** = cosmetic.

### F1 — UI carries a data layer (M)
`streamlit_app.py` (1,552 lines) is better organized than its size suggests — thin action wrappers delegate to `pipeline` correctly (`score_job_now` 8 lines, `set_posting_status_now` 4). The coupling is in the read path: `load_jobs_df` (90 lines), `load_stats_timeseries` (109), `load_job_detail`, and 22 inline `select()` queries put query logic in the view layer, untestable and invisible to the test suite. `render_job_detail` is 237 lines. **Also:** `load_screened_out_df` (30 lines, line 289) is defined and never called — dead code.

### F2 — Two extraction doors, different quality (H)
The same posting URL extracts differently depending on the entry point:
- Pipeline/CLI door: `fanout.fetch_job_from_url` — recognized ATS URLs go through the ATS's own API (structured fields); everything else falls back to `manual.fetch_from_url`.
- UI door: Add-Job uses `manual.fetch_from_url` directly — a Greenhouse URL pasted into the UI is HTML-scraped (manual.py even carries Greenhouse-specific `__NEXT_DATA__` extraction, lines 159–209) instead of using the structured API the pipeline would use.

Return shapes also differ (dict for the UI's editable form vs `Job`/`None` for the pipeline) — legitimately different caller needs, but the *extraction core* should be shared, with two thin adapters. Note: converging the UI onto the ATS-API path changes UI extraction behavior (for the better) — under the two-hats rule that piece is a behavior change to approve separately, not a silent refactor step.

### F3 — Untested surface ≈ 2,300 LOC (H)
Modules with **no direct tests**: `discovery/email_alerts.py` (476), `discovery/linkedin.py` (413), `discovery/manual.py` (367), `stages/discover.py` (301), `batches.py` (186), `cover_letter_doc.py` (279), `output.py` (115), `generation/cover_letter.py` (104). `settings.py` (228) and `scoring/score.py`/`prescreen.py` are covered only indirectly (eval harness + a gitignored live test). Well covered today: `filter.py` (22 tests), `config.py` (16), `fanout.py` (19), `drive_sync.py` (19), `tailor.py` (13), `doctor.py` (10). Any refactor touching the untested list needs characterization tests first (Feathers) — that is Phase 2 below.

### F4 — Dead and dangling references (L)
- `clustering.canonical_and_siblings` (line 108): defined, referenced nowhere in `src/`, `app/`, or `tests`. (Its siblings `cluster_key`, `pick_canonical`, `group_by_cluster` are live.)
- `pipeline.py` docstring points readers to "PLAN.md Phase B1" — PLAN.md was deliberately untracked in `a7a2798` ("untrack personal planning docs"). The pointer dangles for anyone reading the public repo.
- `load_screened_out_df` — see F1.

### F5 — `config.py` hides side effects (M)
The "loader" imports `drive_sync` and `resume_mirror`, runs `load_dotenv` at import time, and `load_profile_files` can trigger a Drive sync / mirror refresh via `_maybe_sync_profile` / `_maybe_refresh_mirror`. With fan-in 8, importing config transitively pulls the Google client stack, and "load the profile" can perform network I/O. The split with `settings.py` is defensible (load+validate vs read accessors) but the side effects inside a load path are the smell — they should be explicit calls from the CLI/stage layer.

### F6 — No lint or format guardrail (M)
`pyproject.toml` configures no linter/formatter (dev deps are pytest only). Style and dead-code drift currently depend on reviewer attention. A minimal `ruff` setup (pyflakes-class rules: unused imports/names, undefined names) would mechanically catch F4-class issues. Deliberately *not* proposing a whole-repo format pass — that violates small-steps.

### F7 — `stages/score.py` has four jobs (M)
465 lines: candidate planning (`_plan_scoring`), prescreen filtering (`_prescreen_filter`), sync scoring loop, and batch submit/fetch — with `batches.py` (186) holding the batch API mechanics beside it. The planning/prescreen/scoring split wants three modules; the boundary with `batches.py` should be drawn once, explicitly.

### F8 — Tier names are string literals in 6 files (M)
"Strong Fit" (and siblings) appear as literals in `stages/score.py`, `stages/generate.py`, `settings.py`, `scoring/prompt.py`, `cli.py`, and `streamlit_app.py`. They agree today, and the eval harness would catch scoring-side drift — but a UI filter or CLI default drifting would fail silently. One constants module (or a `Tier` enum) removes the class.

### F9 — `scripts/` mixes live tools with one-offs (L)
Live tools: `extract_resume.py` (521, the PPTX extractor), `build_profile_template.py` (299), `build_golden_cases.py`, and `drive_auth_local.py` (51) — the last is the Drive re-auth recovery path, referenced by `drive_sync.py` and the CLI's `drive-auth` command (verified 2026-11-24; an earlier draft of this audit misclassified it as a one-off). True one-offs from past migrations: `dedupe_existing.py` (175) and `regen_resume_recs.py` (64), referenced nowhere. Archive those two to `scripts/archive/`.

### F10 — Docs and diagrams drift (M)
- `architecture.mmd`: CLI node lists "run · generate · doctor · sync-profile" — the CLI has 18 commands (score, discover, ingest, prune-closed, batch-fetch, tailor-resume, …). No deployment element at all (muse-vm, cron, tailnet UI — the diagram predates the move).
- `flow.mmd`: core funnel is accurate; omits the fanout search channel, housekeeping (sweep, prune-closed), and the ingest path.
- `OVERVIEW.md`: source list omits Ashby, Workday, and fanout.
- `README.md` (198 lines): documents batches/costs well; never mentions the eval harness, the ingest command, cron/VM deployment, or prune-closed. A new reader cannot discover how the system is actually operated today.

### Not findings (checked, healthy)
`db.py` hub shape; CLI lazy-import pattern; `pipeline.py` re-export shim (one stale docstring line aside); `filter.py`, `clustering.py` core, `scoring/prompt.py` — small, single-purpose, tested. No TODO/FIXME litter anywhere (the TODO hits are the application-answers feature working as designed).

## 3. Consistency pairs that must agree (drift checklist)

| Pair | Status |
|---|---|
| Tier literals across scoring/UI/CLI (F8) | agree today, unguarded |
| UI Add-Job extraction vs pipeline extraction (F2) | **disagree by design accident** |
| `architecture.mmd` CLI list vs `cli.py` | **stale** |
| `flow.mmd` / `OVERVIEW.md` sources vs `stages/discover.py` | **stale (omissions)** |
| README ops story vs crontab/VM reality | **stale** |
| `pipeline.py` docstring vs repo contents (PLAN.md) | **dangling** |
| `settings.py` model defaults vs theo.yaml | agree (yaml wins; defaults are fallback) |
| Eval golden cases vs theo.yaml rules | agree (13/13 prescreen, 2026-11-24) |

## 4. Refactor plan

Rules for every phase: two hats (no behavior change mixed into a refactor commit; bugs found get logged and fixed separately with approval); every commit green (full pytest; eval run after anything touching discovery/prescreen/scoring); one logical change per commit with a why-message; work lands between cron runs, never during the 07:30 window's risk zone without a green suite.

- **Phase A — Dead code & dangling refs (F4, F9).** Remove `canonical_and_siblings`, `load_screened_out_df`; fix the PLAN.md docstring; classify `scripts/` and archive one-offs. *Risk: near zero. Commits: ~4.*
- **Phase B — Guardrails (F6, F8).** Add ruff (pyflakes rules only) + fix what it flags; introduce a tier-constants module and migrate the 6 literal sites one file per commit. *Risk: low. Commits: ~8.*
- **Phase C — Characterization tests (F3).** Pin current behavior for `manual.py` extraction cascade (fixture HTML), `linkedin.py` parsing (fixture payloads), `email_alerts.py` (fixture emails), `batches.py` (mocked API), `settings.py` accessors. No production code changes except testability seams where unavoidable (each its own commit). *Risk: low; this is the phase that buys safety for D–F. Commits: ~6–10.*
- **Phase D — Fetcher convergence (F2).** Mikado graph:
  - Goal: one extraction core; UI and pipeline adapters on top.
    - Extract `extract_posting(url) -> dict` core from manual.py cascade (leaf — pure move).
    - Fanout adapter already delegates; re-point it at the core explicitly (leaf).
    - UI adapter keeps dict shape + editable-form contract (leaf).
    - *Separate decision (behavior change):* UI fetch tries the ATS API first, like the pipeline. Ships as its own feat commit with tests, not inside the refactor.
- **Phase E — Score stage split (F7).** Extract planning, prescreen-filter, and batch-IO from `stages/score.py` into focused modules; `run_score` keeps its signature (callers: CLI, cron wrapper, eval harness, ingest). Mikado leaves: move `_prescreen_filter` first (most self-contained), then planning, then batch glue. Eval run after each move. *Commits: ~5.*
- **Phase F — UI data layer (F1).** Move `load_*` query functions into a `app/queries.py` (or `stages/` read-model module) with tests against a fixture DB; the view keeps rendering only. `render_job_detail` decomposition follows the same seam. Leaves first: `load_stats_timeseries` (self-contained), then `load_jobs_df`, then detail. *Commits: ~6.*
- **Phase G — Config side effects (F5).** Make Drive sync/mirror refresh explicit calls at the CLI/stage layer; `config.py` becomes load+validate only. Touches fan-in 8 — Mikado again, leaves are the individual call sites. *Commits: ~4.*
- **Phase H — Docs reconciliation (F10).** Rewrite `architecture.mmd` command list + add deployment subgraph (VM, cron, tailnet); add fanout/housekeeping/ingest to `flow.mmd`; fix OVERVIEW sources; README gains operations + eval sections. One commit per doc. (No auto-sync machinery — per your Nov 2 call, the .mmd files stay hand-maintained sources; this phase just makes them true.)

**Estimate:** 35–45 commits total across phases, each independently landable and revertible. Phases A–C are the safe core (roughly a third of the work) and already remove most of the "spaghetti" feel; D–G are the structural third; H is an afternoon. Any phase can stop the program with the repo in a better state than it started — there is no point of no return.

## 5. Decisions (Theo, 2026-11-24 — all approved as recommended)

1. **Ruff: adopt.** Pyflakes (F) rules only, configured in `pyproject.toml`; no formatter, no line-length rules. Measured baseline: 23 findings (20 unused imports, 2 unused variables, 1 f-string without placeholders), 21 auto-fixable.
2. **UI fetcher: converge onto the ATS-API path** as a separate `feat:` change after Phase D's shared-core refactor, with the scrape cascade kept as fallback. Not smuggled into a refactor commit.
3. **`scripts/` one-offs: archive**, don't delete — `scripts/archive/` with a short note. (Applies to `dedupe_existing.py` and `regen_resume_recs.py`; `drive_auth_local.py` stays — see F9.)
4. **This audit lives in the repo** at `docs/refactor-audit-2026-11-24.md`, with a progress note appended as phases land.

## 6. Collision protocol (production runs on this checkout)

The compose `dev` service volume-mounts the whole checkout (`.:/workspace`); the daily cron (Mon–Fri 07:30) and the always-on UI container read code live from that tree. Refactor work is therefore isolated:

1. All refactor work happens in a separate git worktree (`~/job-application-agent-refactor`, branch `refactor/program-2026-11`). The production checkout stays on `main`, untouched; tests run in the worktree against fixture DBs.
2. A phase merges into the production checkout only at a chosen moment — never 07:15–07:45 — after confirming no run is in flight (cron log + `docker ps`). Post-merge, in the production checkout: full pytest, the eval harness where the phase touched discovery/prescreen/scoring, then push. Any disagreement reverts before the next cron window.
3. After a merge touching `src/` or `app/`, restart the UI container and verify it serves.
4. The first post-merge cron run's log is read as the phase's final verification.

## 7. Progress log

- **Phase A — done 2026-11-24** (merged to main @ `e471896`, suite 179 green pre- and post-merge, UI restarted and verified). Commits: audit + plan (`dc98110`), remove `canonical_and_siblings` (`fd8a5e0`), remove dead `load_screened_out_df` (`65dec67`), drop PLAN.md docstring reference (`88ded51`), archive one-off scripts (`e471896`). First post-merge verification: the 2026-11-25 07:30 cron run.
- **Phase B — done 2026-11-24.** Ruff adopted (pyflakes-only, `pyproject.toml`) and the tree is at zero findings: the load-bearing re-exports ruff flagged (pipeline's private helpers, the `_common` hub) were made explicit via `__all__` rather than deleted; genuinely unused imports/names removed; `fetch_prescreen_batch`'s unused profile binding became a bare validated load so the gate is preserved. Tier names now live in `job_agent/tiers.py`, migrated at the prompt schema (pinned by a contract test), settings defaults, generate/CLI defaults, and all UI keys and comparisons. Suite 180 green. Note for future gates: the fast-forward merge refused once because main had gained a commit after branching (the Phase A progress note) — progress notes now go on the branch pre-merge, and the branch rebases onto main at the gate.
- **case_13 prescreen flap — resolved 2026-11-24 (Theo approved the fix).** The EPAM golden case flapped ~4/5 approve; rejections anchored on Eindhoven, ignoring the Amsterdam base in the same JD sentence. Theo approved strengthening `geography_rules` with: "If Amsterdam is named anywhere among a posting's locations, the role is in range, regardless of what other cities are also named." Confirmation: 5/5 approvals on resample, full eval prescreen 13/13 and scoring 7/7, France-only case_09 still rejecting. (theo.yaml is gitignored; the eval-results commit carries the record.)
- **Phase E — done 2026-11-24.** `stages/score.py` split verbatim into four modules: `score.py` (run_score only, 465 → 129 lines), `prescreen_filter.py` (the gate), `planning.py` (cluster/replication planning), `score_batch.py` (batch submit + detached fetch). pipeline imports each helper from its owning module; the re-export surface is unchanged. One eval run for the phase at the merge gate instead of one per leaf — the leaves are the same risk class (verbatim moves, suite green per leaf), noted here as a deliberate consolidation of the original plan.
- **Phase D — done 2026-11-24.** The single-URL extraction cluster (PostingGone, ATS URL recognition, URL normalization, ATS-API fetch, scrape fallback) moved verbatim from `fanout.py` to a new `discovery/fetch.py`; fanout re-exports for compatibility, and ingest/discover now import from the canonical home. Separately, as the approved `feat:`: the UI Add-Job form routes recognized ATS URLs through the ATS API (partial-scrape fallback preserved for everything else; PostingGone surfaces as the fetch-failed warning). Suite 236 green throughout.
- **Phase C — done 2026-11-24.** +56 characterization tests (suite 180 → 236): `settings.py` (defaults, merge semantics, tier routing, geography fallbacks), `batches.py` (submission shapes, collectors, sync-retry), `manual.py` parsers (JSON-LD, __NEXT_DATA__, merge/strip semantics, cascade order), `linkedin.py` parsers (id extraction, closed tri-state, cards), `email_alerts.py` parsers (body/URL extraction), `output.py` writers. Two expectations initially written from assumption failed against the real code and were corrected to pin actual behavior (the record_score field set; `_strip_html`'s newline joins) — the tests now document what the code does, not what it was guessed to do. Deferred with reason: `stages/discover.py` (orchestration only, no pure surface — covered indirectly via channel tests), `cover_letter_doc.py` / `generation/cover_letter.py` (docx/prompt assembly; lower refactor risk, revisit if Phase F/G touches them).
- **Phase F — done 2026-11-24.** New `src/job_agent/queries.py` holds the UI read models: `load_stats_timeseries` + `SOURCE_LABELS` (F1), `load_jobs_df` + `_clean_location` (F2), `load_job_detail` (F3) — verbatim moves, now unit-tested against a fixture DB (`tests/test_queries.py`, suite 236 → 240). The app keeps view concerns only: it binds the loaders back with `st.cache_data(ttl=30)` wrappers — except `load_job_detail`, which was uncached in the view and stays uncached (the extraction made that inconsistency visible; caching it is a one-line follow-up). Deliberate scope call: `render_job_detail` was NOT decomposed — view-only code with no test seam; splitting it is churn risk without structural gain, and the F1 finding was the data layer in the view, which is now out.
- **Phase G — done 2026-11-24.** config.py is now load+validate only. G1: the Drive sync + resume-mirror refresh hidden inside `load_profile_files` became the explicit `config.ensure_profile_fresh(profile)`, called at the four generation entry points (stages/generate, stages/single, tailor claim-check, CLI regen-recs); the reader is a pure disk read, pinned by tests. G2: the import-time `load_dotenv` calls became `config.load_env()`, called by cli._bootstrap (its duplicate loads removed), the Streamlit app at startup, and — newly explicit — `evals/run_evals.py` and `scripts/build_golden_cases.py`, which previously got their API key purely from the import side effect. Suite 240 → 243. Fallout fixed along the way: three app imports orphaned by the Phase F moves (a piped ruff call had masked the findings) — `ruff check .` now exits 0 tree-wide, verified unpiped.
- **Phase H — done 2026-11-24.** Docs reconciled with the code: `architecture.mmd` names all 15 CLI commands and gains the muse-vm/cron deployment subgraph; `flow.mmd` gains the fanout sweep, URL ingest, and housekeeping; OVERVIEW's embedded diagram copies were mirrored in the same commits (the duplication is how they drifted), its funnel list and orientation tables now cover Ashby/Workday/fanout/fetch/ingest/queries; README gains Operations (deployment, cron schedule, ingest, prune-closed) and Eval harness sections, and its Drive note matches the explicit sync step.
- **Program complete 2026-11-24.** All eight phases merged to main; suite 179 → 243; `ruff check .` clean; eval series unbroken (final runs: prescreen 13/13, scoring 7/7). The two standing follow-ups this audit surfaced: `load_job_detail` is the only uncached UI loader (one-line fix if wanted), and OVERVIEW.md embeds copies of both diagrams — any future diagram edit must mirror them in the same commit.
- **Follow-ups done + measurement, 2026-11-24.** `load_job_detail` now carries the same ttl=30 cache as the other loaders, and `tests/test_docs_diagrams.py` fails the suite if OVERVIEW's embedded diagram copies drift from the .mmd sources. Measured before/after (baseline = 7433826, the last pre-refactor commit, suite run in a scratch worktree): tests 179 → 244; line coverage of `src/job_agent` under the suite 48% → 59% (new/extracted modules: queries.py 95%, config.py 91%; the live scoring path stays eval-guarded rather than unit-covered — score.py 12% by design of the harness split). Eval agreement across the program's runs stayed inside the pre-refactor variance envelope and ended at its best sample (13/13, 7/7).
- **Phase I — done 2026-11-24** (Theo: "do everything you believe is worth doing"). I-1: `uv.lock` pins all packages; the entrypoint installs via `uv sync --extra dev --locked` (image rebuild required and done); pytest-cov added to dev extras. I-2: GitHub Actions CI (`.github/workflows/ci.yml`) — locked install, `ruff check .`, suite with a 64% coverage floor (a ratchet at the measured level, per the directional-coverage decision). Proven in a cleanroom clone (no .env / profile / API key) before landing; the cleanroom caught two environment-dependent tests (a Phase G test asserting a real-profile artifact, and a truthfulness test implicitly requiring ANTHROPIC_API_KEY), both made hermetic. I-3: UI write actions moved to `job_agent/mutations.py` with tests — the view now touches the DB only through queries/mutations/pipeline. I-4: nine characterization tests for `stages/discover.py` pinning its counting contract and channel error isolation. Suite 244 → 257; coverage 59% → 64%.
