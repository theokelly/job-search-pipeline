# Job Application Agent

Agentic pipeline for automating job discovery, scoring, and application material generation. See `HANDOFF.md` for full project context and architecture.

---

## Daily usage

### Start the container

```bash
# --service-ports publishes port 8501 so the Streamlit UI is accessible on your Mac
docker compose run --rm --service-ports dev
```

The container runs as `claude-user`. On first start the venv is created and dependencies installed automatically; the Streamlit UI starts in the background at **http://localhost:8501**. Then launch Claude Code:

```bash
claude --dangerously-skip-permissions
```

### Run the pipeline

```bash
# Full pipeline: LinkedIn search + ATS companies + score + generate materials
.venv/bin/job-agent run --linkedin --email-days 14

# With Gmail alert ingestion (requires app password in .env — see Gmail setup below)
.venv/bin/job-agent run --linkedin --email --email-days 14
```

Discovers new jobs → scores unscored jobs → generates cover letters and resume recs for **Strong Fit only** (default). Worth Reviewing jobs are scored and visible in the UI — generate their materials on demand with the UI's "Generate Materials" button, or pass `--tiers "Strong Fit,Worth Reviewing"` to bulk-generate. Output lands in `outputs/theo/`.

### Open the UI

```bash
bash /workspace/run_ui.sh
```

Then open **http://localhost:8501** in your browser.

### Run steps individually

```bash
.venv/bin/job-agent discover              # fetch and save new postings
.venv/bin/job-agent discover --dry-run    # preview without saving
.venv/bin/job-agent discover --priority high   # high-priority companies only
.venv/bin/job-agent prune-closed          # mark LinkedIn jobs showing "No longer accepting applications" as closed
.venv/bin/job-agent prune-closed --limit 50    # check only the first 50 (faster spot-check)
.venv/bin/job-agent score                 # score all unscored active jobs
.venv/bin/job-agent score --limit 10      # score a small batch
.venv/bin/job-agent score --rescore --rescore-tiers "Strong Fit,Worth Reviewing"   # fresh scores for jobs whose latest tier is listed (after profile changes)
.venv/bin/job-agent generate              # generate materials for pending Strong Fit jobs
.venv/bin/job-agent generate --tiers "Strong Fit,Worth Reviewing"   # include WR tier
.venv/bin/job-agent status                # DB stats and tier breakdown
.venv/bin/job-agent doctor                # health check: profile, env, file sync status (read-only)
.venv/bin/job-agent sync-profile          # dry-run Drive → local sync for profile files (add --apply to write)
.venv/bin/job-agent sync-profile --file starls --verbose   # one file, full diff
.venv/bin/job-agent tailor-resume outputs/theo/2026-10-07_acme_data-scientist   # apply rec edits to PPTX copy
```

### Output structure

Each application lives in `outputs/theo/{date}_{company}_{role}/`:

| File | Contents |
|---|---|
| `job.md` | Source, URL, scores, and JD preview — the quick-glance summary |
| `cover_letter.md` | Ready-to-use draft; make minor edits then paste |
| `resume_recommendation.md` | Which version to use + 1-3 surgical text edits |
| `score.json` | Tier, scores, rationale, gaps, positioning notes |

---

## First-time setup

### 1. Build and start the container

```bash
docker compose build
docker compose run --rm --service-ports dev
```

The container runs as `claude-user` (non-root). Port 8501 is mapped to your Mac for the Streamlit UI.

### 2. Authenticate Claude Code

Inside the container:

```bash
claude login
```

This opens a URL — authenticate in your Mac browser. The session is stored in the `claude-auth` named Docker volume and persists across container runs — even with `--rm`. You only need to authenticate once.

### 3. Configure secrets

```bash
cp .env.example .env
# Edit .env and fill in:
#   ANTHROPIC_API_KEY=sk-ant-...
#   GMAIL_USER=your@gmail.com          (optional — for email alert ingestion)
#   GMAIL_APP_PASSWORD=xxxx xxxx xxxx xxxx
```

### 4. Initialize the database

```bash
.venv/bin/job-agent init-user
```

---

## Gmail alert ingestion

Uses IMAP with a Gmail App Password — no Google Cloud project required.

1. In Gmail, go to **Manage your Google Account → Security → 2-Step Verification → App passwords**.
2. Generate an app password for "Mail".
3. Set in `.env`:
   ```
   GMAIL_USER=your@gmail.com
   GMAIL_APP_PASSWORD=xxxx xxxx xxxx xxxx
   ```
4. In Gmail, create label filters:
   - From `jobalerts@linkedin.com` → label `Job Alerts/LinkedIn`
   - From `jobalert@indeed.com` → label `Job Alerts/Indeed`
   - From `alerts@glassdoor.com` → label `Job Alerts/Glassdoor`
5. Use `--email` flag when running the pipeline.

---

## Streamlit UI

Browse all jobs, review scores and generated materials, track application status, and override screened-out jobs.

Start with `bash /workspace/run_ui.sh`, then open **http://localhost:8501**.

Features:
- **Jobs view** — filterable table (tier, status, source, free-text search); click any row for full detail. Tier filter includes Unscored and Screened Out.
- **Detail view** — score breakdown, cover letter and resume rec tabs, one-click status tracking (Applied / Interviewing / Offer / Rejected / Not Pursuing)
- **Override & Score** — trigger Sonnet scoring in-app for any screened-out or unscored job
- **Generate Materials** — generate cover letter + resume rec in-app for any scored job
- **Stats view** — totals by tier, source, and company

---

## Adding new companies

Edit `profile/theo.yaml` under `target_companies`. Set `greenhouse_token` or `lever_slug` if known. Leave both `null` for companies on Workday/own ATS — they'll be covered by Gmail alerts.

To find a Greenhouse token: check `https://boards.greenhouse.io/{token}`. To find a Lever slug: check `https://jobs.lever.co/{slug}`.

---

## Profile updates

| File | When to update |
|---|---|
| `profile/theo.yaml` | Add companies, tweak dealbreakers/rubric, update target roles (validated on load — typos fail fast) |
| `profile/narrative_guidance.md` | Refine positioning, add new framing, adjust what to emphasize |
| `profile/resume_versions/ai_focused.md` | Regenerate from PPTX: `scripts/extract_resume.py profile/resume_versions/TheoKelly_CV_Amsterdam.pptx profile/resume_versions/ai_focused.md --check` |
| `profile/accomplishments/starls.md` | Auto-synced from Google Drive (see below) |
| `profile/accomplishments/brag_sheet.md` | Auto-synced from Google Drive (see below) |
| `profile/voice/elevator_pitch.md` | Auto-synced from Google Drive (see below) |
| `profile/voice/cover_letter_examples.md` | Add approved letters as few-shot examples |

---

## Google Drive sync

`starls.md`, `brag_sheet.md`, and `elevator_pitch.md` are synced from Google Docs (Drive is the source of truth). Google Docs are converted to markdown via the Docs API, preserving headings, bold/italic, and lists.

```bash
.venv/bin/job-agent sync-profile              # dry-run: show what would change
.venv/bin/job-agent sync-profile --apply      # write Drive content to local files
```

With `profile_sync.sync_on_load: true` in `theo.yaml`, generation (`job-agent generate` and the UI's Generate button) auto-syncs first — skipped if synced within the last 15 minutes, and never breaks generation if Drive is unreachable.

One-time OAuth setup: create a Desktop-app OAuth client in Google Cloud Console (enable Drive API + Docs API), save the client secrets to `data/drive_client.json`, then run `python3 scripts/drive_auth_local.py` on your Mac (needs a browser). Token lands in `data/drive_token.json`. File mappings live in the `profile_sync:` block of `theo.yaml`.

---

## Resume tailoring

`job-agent tailor-resume` applies a generated `resume_recommendation.md` to a copy of the base resume PPTX — swaps and in-section bullet reorders, with formatting preserved:

```bash
.venv/bin/job-agent tailor-resume outputs/theo/2026-10-07_acme_data-scientist
.venv/bin/job-agent tailor-resume outputs/theo/2026-10-07_acme_data-scientist --dry-run   # preview only
.venv/bin/job-agent tailor-resume outputs/theo/2026-10-07_acme_data-scientist --no-upload # skip Drive
```

Writes `resume_tailored.pptx` next to the recommendation and flags any edit that might have overflowed its text frame for a quick visual check (exit code 2 when flagged). A human review pass is still expected — this removes the mechanical toil, not the judgment.

**Two layers, kept in sync.** The `.md` files under `profile/resume_versions/` are the *text* layer (what the LLM reads; character budgets for layout-safe suggestions come from here). The base PPTX is the *layout* layer (what gets edited). `job-agent doctor` runs `extract_resume.py --check` to verify they match — regenerate the `.md` from the PPTX when the base changes.

**Drive flow** (`tailoring:` block in `theo.yaml`):

- `base_pptx_drive_id` — a static "latest base" PPTX in Drive. Update it in place whenever the base changes; `tailor-resume` downloads it instead of using the local `pptx_map`.
- `output_parent_folder_id` — the Drive folder holding your `YYYY_MM` month folders. The tailored PPTX uploads to the current month's folder (created if missing) as `TheoKelly_CV_{company}_{YYYY-MM-DD}.pptx`.
- `upload_to_drive: true` — upload by default; `--no-upload` skips it. The local copy in the output dir is always written.

---

## Cost reference

| Operation | Model | Est. cost (batched) |
|---|---|---|
| Prescreen 1 job | Haiku 4.5 | ~$0.0005 |
| Score 1 job | Sonnet 4.6 | ~$0.005 |
| Generate 1 cover letter (Strong Fit) | Opus 4.7 | ~$0.07 |
| Generate 1 cover letter (Worth Reviewing, on demand) | Sonnet 4.6 | ~$0.04 |

Prescreen and scoring run through the Batches API by default (50% discount on input + output). Note: prompt-cache hits inside a batch are best-effort and usually **miss** (requests process concurrently), so the batch discount — not caching — is the main saving on the batched path. Caching does help the synchronous paths (UI "score now", `--no-batch`) when calls land within the 5-minute cache TTL.

Materials are auto-generated for Strong Fit only; Worth Reviewing letters are generated per-job from the UI, which historically cuts generation volume ~8×.

---

## Running tests

```bash
# Fast unit tests (no API calls)
.venv/bin/pytest tests/test_pre_filter.py tests/test_scoring_contract.py -v

# Live calibration test (requires ANTHROPIC_API_KEY)
.venv/bin/pytest tests/test_scoring_calibration.py -v -s
```

---

## Secrets and gitignored paths

`.env`, `data/` (SQLite DB), and `outputs/` (generated materials) are all gitignored.

```bash
cp .env.example .env
# Fill in ANTHROPIC_API_KEY and optionally Gmail credentials
```
