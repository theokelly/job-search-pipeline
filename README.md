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
    RUN["<b>job-agent run</b>: sweep → discover → prune closed → score<br/><i>generation is on demand</i>"]

    subgraph SRC[Sources]
        LI[LinkedIn search]
        ATS[ATS boards<br/>Greenhouse · Lever · Ashby · Workday]
        EM[Gmail job alerts]
        MAN[Manual URL<br/>via Streamlit]
    end

    LI --> INT
    ATS --> INT
    EM --> INT
    MAN --> INT
    RUN -.->|orchestrates| INT

    INT["1 · Intake &amp; normalize<br/><i>fetch · normalize · upsert as Job rows</i>"]
    F["2 · Pre-filter<br/><i>rules · free — dealbreakers / role / geography</i>"]

    INT --> F
    F -->|kept| PS
    F -.->|dropped| X1((discarded<br/>not stored))

    subgraph CLUS[Cluster-aware execution — canonical posting only, results replicate to siblings]
        PS["3 · Prescreen<br/><i>Haiku · binary gate · batch or sync</i>"]
        SC["4 · Score<br/><i>Sonnet · tier + sub-scores + gaps + positioning</i>"]
        GEN["5 · Generate application packet<br/><i>on demand · cover letter Opus/Sonnet · resume rec + answers + grounding Haiku</i>"]
        PS -->|approved| SC
        SC -->|selected tiers| GEN
    end

    PS -.->|rejected| X2((screened out<br/>reason stored))

    PROF["Profile control plane<br/><i>target companies · dealbreakers · geography · rubric<br/>narrative · resumes · STAR · voice · model + batch settings</i>"]
    PROF -.->|configures| F
    PROF -.->|configures| PS
    PROF -.->|configures| SC
    PROF -.->|configures| GEN

    DB[("SQLite state<br/>Job · Score · UserJobMeta · Application")]
    SC -.->|scores stored| DB
    GEN -.->|application row| DB

    OUT["Application packet — outputs/&lt;user&gt;/&lt;date&gt;_&lt;company&gt;_&lt;role&gt;/<br/><i>job.md · cover_letter.md · resume_recommendation.md<br/>answers.md · grounding_check.md · score.json</i>"]
    GEN --> OUT
    DB -.->|materials_path| OUT

    HK["Housekeeping<br/><i>sweep: stale → expired · prune: closed → closed</i>"]
    HK -.->|maintains Job.status| DB

    HUM["Human application workflow — Streamlit<br/><i>review · edit · tailor PPTX · Drive Doc · download packet<br/>record status (propagates across cluster)</i>"]
    OUT --> HUM
    HUM -.->|manual submission only| EMP[Employer ATS]
    HUM -.->|record status| DB
    HUM -.->|Add Job · score now · generate now| INT

    classDef free fill:#e8f5e9,stroke:#2e7d32,color:#1b5e20;
    classDef cheap fill:#fff8e1,stroke:#f9a825,color:#7f6000;
    classDef mid fill:#fff3e0,stroke:#ef6c00,color:#7f3e00;
    classDef expensive fill:#fce4ec,stroke:#c2185b,color:#7a0c37;
    classDef human fill:#e3f2fd,stroke:#1565c0,color:#0d3c78;
    classDef store fill:#f3e5f5,stroke:#6a1b9a,color:#3e0764;
    classDef ctrl fill:#eceff1,stroke:#546e7a,color:#263238;

    class INT,F free;
    class PS cheap;
    class SC mid;
    class GEN expensive;
    class HUM human;
    class DB store;
    class PROF,RUN,HK ctrl;
```

*System architecture:*

```mermaid
flowchart TB
    THEO["Theo<br/><i>operator · reviewer · applicant</i>"]

    subgraph EXT[External systems]
        DRV["Google Drive / Docs<br/><i>profile files · base PPTX · monthly folders</i>"]
        SRC["Job sources<br/><i>LinkedIn · ATS boards · Gmail alerts</i>"]
        ANT["Anthropic API<br/><i>Haiku · Sonnet · Opus · Batches + sync</i>"]
        EMP["Employer ATS<br/><i>outside the system</i>"]
    end

    subgraph SYS[Local Docker workspace — single user]
        subgraph IFACE[Interface layer]
            UI["Streamlit operator console<br/><i>Jobs · Add Job · Stats</i>"]
            CLI["job-agent CLI<br/><i>batch operations · utilities</i>"]
        end

        CODE["Shared Python package<br/><i>direct imports — no separate API service</i>"]

        subgraph OPS[Application operations]
            BUILD["Artifact builders<br/><i>tailor PPTX · cover-letter Doc</i>"]
            DISC["Discovery<br/><i>ingest · sweep · prune</i>"]
            SCORE["Scoring<br/><i>pre-filter · prescreen · score</i>"]
            GEN["Generation<br/><i>application packet</i>"]
        end

        PROF["Profile &amp; source material<br/><i>theo.yaml · rubric · narrative · resumes · STAR · voice</i>"]

        subgraph STATE[State &amp; artifacts]
            DB[("SQLite — data/jobs.sqlite<br/>User · Job · Score · Application · UserJobMeta")]
            OUT["outputs/<br/><i>application packets</i>"]
            LOG["data/runs.log<br/><i>per-stage run records, written by stage wrappers</i>"]
        end
    end

    THEO --> UI
    THEO --> CLI
    THEO -->|submits manually| EMP
    UI --> CODE
    CLI --> CODE
    CODE --> DISC
    CODE --> SCORE
    CODE --> GEN
    CODE --> BUILD

    PROF -.->|configures| DISC
    PROF -.->|configures| SCORE
    PROF -.->|configures| GEN

    SRC --> DISC
    ANT --> SCORE
    ANT --> GEN
    DRV <-->|profile sync| PROF
    DRV <-->|base PPTX down · documents up| BUILD

    DISC --> DB
    SCORE --> DB
    GEN --> DB
    GEN --> OUT
    BUILD --> OUT
    DB -.->|materials_path| OUT

    UI <-.->|browse · edit · track| DB
    UI -.->|read · download| OUT

    classDef human fill:#e3f2fd,stroke:#1565c0,color:#0d3c78;
    classDef iface fill:#e8f5e9,stroke:#2e7d32,color:#1b5e20;
    classDef ops fill:#fff3e0,stroke:#ef6c00,color:#7f3e00;
    classDef store fill:#f3e5f5,stroke:#6a1b9a,color:#3e0764;
    classDef ctrl fill:#eceff1,stroke:#546e7a,color:#263238;
    classDef ext fill:#fafafa,stroke:#616161,color:#212121;

    class THEO,UI human;
    class CLI iface;
    class DISC,SCORE,GEN,BUILD ops;
    class DB,OUT,LOG store;
    class PROF,CODE ctrl;
    class SRC,ANT,DRV,EMP ext;
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
