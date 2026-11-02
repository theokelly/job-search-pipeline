# Job Search Pipeline

Agentic pipeline for job discovery, fit scoring, and application-material generation. The job search is a funnel — finding postings, reading them, scoring fit, drafting letters — and most of it is toil. This automates the toil. The judgment stays human: nothing submits an application.

## How it works

**Discover.** Pulls postings from LinkedIn, Greenhouse / Lever / Ashby / Workday ATS APIs, Gmail job alerts, and manual adds. Deterministic rule pre-filters drop obvious non-fits before any LLM call is spent.

**Prescreen.** Haiku does a cheap binary fit check on everything that survives the filters.

**Score.** Sonnet scores fit structurally — skill fit, preference alignment, gaps, positioning notes — into tiers: Strong Fit, Worth Reviewing, Long Shot, Skip. Runs through the Batches API at a 50% discount.

**Generate.** On demand, per job: cover letter (Opus), resume recommendations, application-form answers, and a grounding check that verifies every claim in the letter against your materials. Generation is on-demand because the Opus calls are the expensive part — no point drafting letters for jobs you'll never touch.

**Diagrams** — Mermaid sources in [flow.mmd](flow.mmd) and [architecture.mmd](architecture.mmd):

*Job-processing funnel:*

```mermaid
flowchart TD
    subgraph SRC[Sources]
        LI[LinkedIn search]
        GH[Greenhouse ATS]
        LV[Lever ATS]
        AS[Ashby ATS]
        WD[Workday ATS]
        EM[Gmail job alerts]
        MAN[Manual entry]
    end

    LI --> F
    GH --> F
    LV --> F
    AS --> F
    WD --> F
    EM --> F
    MAN --> F

    F["1 · Pre-filter<br/><i>rules · free</i>"]
    CL["2 · Cluster<br/><i>company + title key</i>"]
    PS["3 · Prescreen<br/><i>Haiku · cheap gate</i>"]
    SC["4 · Score<br/><i>Sonnet · assign tier</i>"]
    GEN["5 · Generate<br/><i>Opus · on demand</i>"]
    UI["6 · Review &amp; apply<br/><i>Streamlit · operator console</i>"]

    F -->|kept| CL
    F -.->|dropped: dealbreaker /<br/>role / geography| X1((discarded))
    CL -->|one canonical<br/>per cluster| PS
    PS -->|approved| SC
    PS -.->|rejected| X2((skipped))
    SC -->|Strong Fit| GEN
    SC -.->|Worth Reviewing /<br/>Long Shot / Skip| DB[(SQLite DB)]
    GEN -->|cover letter +<br/>resume rec| UI

    CL -. "results replicate<br/>to sibling postings" .-> SIB[/sibling jobs/]

    F --> DB
    SC --> DB
    GEN --> DB
    UI <--> DB
    UI -.->|on-demand override:<br/>score / generate now| SC

    classDef free fill:#e8f5e9,stroke:#2e7d32,color:#1b5e20;
    classDef cheap fill:#fff8e1,stroke:#f9a825,color:#7f6000;
    classDef mid fill:#fff3e0,stroke:#ef6c00,color:#7f3e00;
    classDef expensive fill:#fce4ec,stroke:#c2185b,color:#7a0c37;
    classDef human fill:#e3f2fd,stroke:#1565c0,color:#0d3c78;
    classDef store fill:#f3e5f5,stroke:#6a1b9a,color:#3e0764;

    class F,CL free;
    class PS cheap;
    class SC mid;
    class GEN expensive;
    class UI human;
    class DB store;
```

*System architecture:*

```mermaid
flowchart TD
    OP["Operator<br/><i>reviews · edits · submits</i>"]

    subgraph DRV[Google Drive]
        direction TB
        APP["Application packets<br/><i>tailored resume · cover letter</i>"]
        REF["Living Reference Docs<br/><i>profile · STAR stories · narrative · rubric</i>"]
        APP ~~~ REF
    end

    EMP["Employer ATS"]
    OP -.->|edits| APP
    APP -->|submitted manually| EMP

    UI["Streamlit operator console<br/><i>Jobs · Add Job · Stats</i>"]
    CLI["job-agent CLI<br/><i>run · generate · doctor · sync-profile</i>"]
    OP --> UI
    OP --> CLI

    PIPE["Pipeline package<br/><i>discovery · scoring · generation · builders</i>"]
    UI --> PIPE
    CLI --> PIPE

    SRC["Job sources<br/><i>LinkedIn · ATS boards · Gmail</i>"]
    ANT["Anthropic API<br/><i>Haiku · Sonnet · Opus</i>"]
    SRC --> PIPE
    ANT --> PIPE
    REF -.->|configures| PIPE

    DB[("SQLite DB<br/>jobs · scores · applications")]
    OUT["Application packet<br/><i>drafts in outputs/</i>"]
    PIPE --> DB
    PIPE --> OUT
    DB -.->|materials_path| OUT
    UI <-->|browse · edit · track| DB
    OUT -->|final documents| APP

    classDef mid fill:#fff3e0,stroke:#ef6c00,color:#7f3e00;
    classDef human fill:#e3f2fd,stroke:#1565c0,color:#0d3c78;
    classDef store fill:#f3e5f5,stroke:#6a1b9a,color:#3e0764;

    class PIPE mid;
    class OP,UI human;
    class DB,OUT,APP store;
```

Every application lands in `outputs/<user>/{date}_{company}_{role}/`:

| File | Contents |
|---|---|
| `job.md` | Source, URL, scores, JD preview — the quick-glance summary |
| `cover_letter.md` | Draft letter; edit, then paste |
| `resume_recommendation.md` | Which resume version to use + surgical text edits |
| `answers.md` | Pre-drafted application-form answers; `TODO` where the profile can't answer |
| `grounding_check.md` | Claim-by-claim verification of the letter against your materials; `REVIEW` flags for human eyes |
| `score.json` | Tier, scores, rationale, gaps, positioning notes |

## Resume tailoring

`job-agent tailor-resume` applies a `resume_recommendation.md` to a copy of your base resume PPTX — text swaps and in-section bullet reorders, formatting preserved — and flags anything that might have overflowed its text frame (exit code 2). It removes the mechanical toil, not the review pass.

Two layers, kept in sync: the `.md` files under `profile/resume_versions/` are the text layer the LLM reads (with character budgets so suggestions stay layout-safe); the PPTX is the layout layer. `job-agent doctor` verifies the two match.

## Quickstart

Prerequisites: Docker and an Anthropic API key.

```bash
git clone https://github.com/theokelly/job-search-pipeline.git
cd job-search-pipeline
cp .env.example .env                          # add your ANTHROPIC_API_KEY
cp profile_template/example.yaml profile/theo.yaml   # your profile (gitignored)
docker compose build
docker compose run --rm --service-ports dev
```

The container builds a venv and installs dependencies on first start. Inside it:

```bash
job-agent run --linkedin --email-days 14   # discover new jobs, score the unscored
job-agent generate                         # materials for Strong Fit jobs
job-agent tailor-resume outputs/<user>/<date>_<company>_<role>
job-agent doctor                            # health check: profile, env, file sync
```

Bulk generate skips jobs you've already marked applied / rejected / not pursuing — pass `--force-all-status` to include them.

Prefer a UI? `bash run_ui.sh` starts Streamlit at http://localhost:8501 — review scores, generate materials per job, and tailor resumes from the browser.

## The profile

Everything personal lives in gitignored `profile/`: resume versions, STAR stories, cover-letter principles, target roles and geography, scoring preferences, and vetted application answers. `profile_template/example.yaml` documents the full schema — copy it and fill in your own. (It was generated from a real profile by `scripts/build_profile_template.py`, which redacts identity, secrets, and personal answers; re-run it after changing your profile.)

## Cost

Measured costs with batching:

| Operation | Model | Est. cost |
|---|---|---|
| Prescreen 1 job | Haiku | ~$0.0005 |
| Score 1 job | Sonnet | ~$0.005 |
| Cover letter (Strong Fit) | Opus | ~$0.07 |
| Cover letter (Worth Reviewing) | Sonnet | ~$0.04 |
| Answers + grounding check | Haiku | ~$0.01 |

Materials generate for Strong Fit only by default; Worth Reviewing letters are per-job from the UI. That cuts generation volume ~8x.

## Design notes

- **Human in the loop.** The pipeline drafts; you review, edit, and submit. End-to-end automation is deliberately off the table.
- **Batches API for scoring.** 50% cheaper than synchronous calls. Prompt-cache hits inside a batch are best-effort (concurrent requests usually miss), so the discount — not caching — is the real saving.
- **Drive as source of truth (optional).** STAR stories and brag sheet can sync from Google Docs; generation auto-syncs with a 15-minute staleness guard and never breaks when Drive is unreachable.

`OVERVIEW.md` has the architecture quick reference.

## License

MIT — see [LICENSE](LICENSE).
