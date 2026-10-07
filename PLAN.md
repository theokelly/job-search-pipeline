# Work Plan — Architecture Cleanup + Material-Update Automation

Sketched 2026-10-07. Sequenced so each phase de-risks the next.
Status: **Complete.** Phases A–C finished and committed 2026-10-07
(A: `7aed207`, B1: `52cd524`, B2–B6+C2: `e875765`, C3: commit, C1+auto-sync: commit).
The phases below are kept as a historical record of what was done.

---

## Phase A — Safety net (~30 min, manual)

1. `.gitignore` written (based on `job-application-pipeline-public/.gitignore`,
   plus `backups/` and `node_modules/`).
2. Run on the Mac:
   ```bash
   cd /Users/theokelly/Documents/Personal_Python/claude-projects/job-application-agent
   git init -b main
   git status --short | head -40   # profile/, .env, data/, outputs/, HANDOFF.md must NOT appear
   git add -A
   git commit -m "Initial commit: pre-refactor snapshot"
   git tag pre-refactor
   ```
3. Create **private** GitHub repo `job-application-agent`, push `main` + tags.
4. Leave `job-application-pipeline-public/` untouched (contains a real `.env` on disk).

---

## Phase B — Architecture cleanup (~half a day)

### B1. Split `pipeline.py` (~980 lines) into stage modules

`pipeline.py` becomes a thin re-export shim so existing imports keep working:

| New module | Functions moved |
|---|---|
| `stages/_common.py` | `_default_echo`, `_replicate_score`, `PROFILE_PATH_DEFAULT`, `NARRATIVE_PATH` |
| `stages/discover.py` | `run_discover` |
| `stages/housekeeping.py` | `run_sweep`, `run_prune_closed`, `_RateLimiter` |
| `stages/score.py` | `run_score`, `_plan_scoring`, `_prescreen_filter`, `_score_batch`, `fetch_score_batch`, `fetch_prescreen_batch` |
| `stages/generate.py` | `run_generate` |
| `stages/single.py` | `score_one`, `generate_one` |

Import contract to preserve:

- `cli.py` imports: `run_discover`, `run_sweep`, `run_score`, `run_generate`, `run_prune_closed`
- `app/streamlit_app.py` imports: `score_one`, `generate_one`

Also fix `cli.py run`: hardcoded `load_profile("profile/theo.yaml")` (line ~124)
— derive from `--user` / env instead (already flagged in GENERALIZATION.md).

### B2. `datetime.utcnow()` sweep

~22 sites across `src/` and `app/` (known: `run_sweep`, `run_prune_closed`,
`_prescreen_filter`, `fetch_prescreen_batch`, `score_one` in pipeline.py).
Replace with `datetime.now(timezone.utc).replace(tzinfo=None)` — the DB stores
naive datetimes, so a blind switch to aware datetimes breaks `last_seen < cutoff`
comparisons against existing rows.

### B3. Batch sibling-replication gap

`_score_batch` timeout handler tells the user batch-fetch won't auto-replicate
scores to cluster siblings. Fix: make `fetch_score_batch` replicate via
`clustering.group_by_cluster` (same pattern as the sync path), or record the
`replications` map alongside the batch id so fetch can replay it.

### B4. Validate `theo.yaml` at load

Add a pydantic model in `config.py`; fail fast on bad config instead of
mid-run. Covers `target_companies` entries, `models`, `truncation`, `pipeline`,
`batches`, `geography`, `prescreen` blocks.

### B5. Title-filter `"swe"` substring quirk

In `filter.py`: `"swe"` substring-matches e.g. "Sweden" in titles. Switch title
filter terms to word-boundary matching.

### B6. Per-run structured logging

Run id + stage timings + counts, e.g. JSON lines to `data/runs.log`.
Replaces ad-hoc `echo` as the only observability. Keep the `echo` callable
signature for Streamlit/tests.

**Verification:** offline tests green
(`test_pre_filter`, `test_scoring_contract`, `test_clustering_geo`,
`test_prune_closed`, `test_rescore_plan`); `job-agent discover --dry-run`,
`score --limit 2`, `generate` smoke run; Streamlit imports unchanged.

---

## Phase C — Automating material updates (~half a day)

### C1. Google Drive sync (kills manual snapshot toil)

These `profile/` files are manual snapshots of Drive originals (per HANDOFF.md):
`accomplishments/starls.md`, `accomplishments/brag_sheet.md`,
`voice/elevator_pitch.md`.

- Add `job-agent sync-profile` using the Drive API (OAuth, `google-api-python-client`).
- Map Drive file IDs → repo paths in `theo.yaml` (new `profile_sync:` block).
- Manual trigger first; auto-sync at the start of `job-agent run` once trusted.
- Export as plain text / markdown; report a diff summary, never silent-overwrite
  without showing what changed.

### C2. Resume PPTX → markdown extraction

`profile/resume_versions/*.md` are hand-synced from PPTX (README lists this as
manual toil). Add `scripts/extract_resume.py` using `python-pptx` that
regenerates the `.md` files from the PPTX sources. Run it from `sync-profile`
(or standalone) whenever the PPTX changes.

### C3. Staleness check

`job-agent doctor` (or extend `status`): compares mtimes / Drive `modifiedTime`
for each synced file and reports what's stale. Cheap early win — build this
first if Drive OAuth turns fiddly.

**Verification:** `sync-profile --dry-run` shows diffs; extraction output matches
current `.md` files byte-for-byte before switching over; `doctor` flags a
known-stale file.

---

## Out of scope for this plan (future)

- Layer 4 auto-applying (Playwright per-ATS adapters) — needs explicit go-ahead;
  recommend one-click-apply from UI over fully autonomous submission.
- LinkedIn networking automation — account-ban risk; recommend draft-outreach
  pattern (pipeline finds people + drafts, human sends).
- Public-repo generalization (see GENERALIZATION.md) — separate project.
