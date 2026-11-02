# Job Search Pipeline — Quick Reference

*A conceptual overview for someone new to the project. This explains **what it does and how it works**, not how to operate it — see `README.md` for commands and setup.*

---

## The concept in one paragraph

This is an agentic pipeline that automates the top of a job-search funnel. It continuously
**discovers** job postings from multiple sources, **filters and scores** them against one
person's profile using LLMs, and for the best matches **generates tailored application
materials** (a cover letter and a resume-tweak recommendation). A human then reviews the
results in a web UI, tracks which jobs they've applied to, and can trigger scoring or
generation on demand. The design goal is to spend expensive LLM calls only where they add
value — cheap filters weed out obvious non-matches first, and near-duplicate postings are
collapsed so each real opportunity is evaluated once.

---

## The funnel

The pipeline is a cost-descending funnel: each stage is more expensive per job than the
last, so each stage tries to pass as few jobs as possible to the next.

```
   SOURCES                     A stage's job is to
 ┌──────────────────┐          shrink the pool cheaply
 │ LinkedIn search  │          before the pool hits a
 │ Greenhouse ATS   │          more expensive stage.
 │ Lever ATS        │
 │ Gmail job alerts │
 │ Manual entry     │
 └────────┬─────────┘
          │  raw postings
          ▼
 ┌──────────────────────────────┐   Rule-based, FREE.
 │ 1. PRE-FILTER                │   Drops on dealbreaker keywords,
 │    (rules, no LLM)           │   wrong role type, or location
 └────────┬─────────────────────┘   outside target geography.
          │  kept postings → saved to DB
          ▼
 ┌──────────────────────────────┐   Collapses near-duplicate postings
 │ 2. CLUSTER                   │   (same role broadcast across many
 │    (company + title key)     │   cities) into ONE canonical job.
 └────────┬─────────────────────┘   Siblings inherit results later.
          │  one canonical per cluster
          ▼
 ┌──────────────────────────────┐   Haiku (cheapest LLM). A fast
 │ 3. PRESCREEN                 │   yes/no gate. Rejects obvious
 │    (Haiku)                   │   non-fits before real scoring.
 └────────┬─────────────────────┘
          │  approved jobs
          ▼
 ┌──────────────────────────────┐   Sonnet. Assigns a fit TIER +
 │ 4. SCORE                     │   skill/preference scores, gaps,
 │    (Sonnet)                  │   positioning notes, rationale.
 └────────┬─────────────────────┘   Tiers: Strong Fit / Worth
          │  scored jobs            Reviewing / Long Shot / Skip.
          ▼
 ┌──────────────────────────────┐   Opus (most expensive). Runs
 │ 5. GENERATE                  │   only for Strong Fit by default.
 │    (Opus)                    │   Produces cover letter +
 └────────┬─────────────────────┘   resume recommendation.
          │  application materials
          ▼
 ┌──────────────────────────────┐   Human reviews, edits, applies,
 │ 6. REVIEW (Streamlit UI)     │   and tracks outcome. Can override
 │    (human in the loop)       │   filters / score / generate on demand.
 └──────────────────────────────┘
```

Once a stage's result is computed for the canonical job in a cluster, it is **replicated**
onto all the sibling postings for free — so the UI and queries treat each city's posting as
scored, but the LLM only paid once.

### Rendered flow

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

*Solid arrows are the happy path; dashed arrows are exits, configuration, and human control loops.
Node colours track cost, from free (green) → Haiku (yellow) → Sonnet (orange) → Opus (pink).*

---

## System architecture

The funnel above is the *processing* view — what happens to one posting. This is the
*system* view: two peer entry points (the Streamlit console and the CLI) over one
shared Python package, the profile acting as a control plane, and external systems
at the edges. There is no API service between the UI and the pipeline: both entry
points import the same code directly, and Streamlit additionally reads and writes
the database and `outputs/` itself for browsing, tracking, and downloads.

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

---

## Key ideas that make it work

**Clustering (dedupe by opportunity, not by posting).** The same role is often posted across
4–6 cities with distinct IDs. A `cluster_key` derived from *company + title* (deliberately
ignoring location) groups them. The pipeline picks one "canonical" posting per cluster —
preferring the best-located variant — scores/generates for it once, and copies the outcome to
its siblings. This is the single biggest cost saver.

**Cheapest-model-first gating.** Rules → Haiku → Sonnet → Opus. Each layer removes work from
the more expensive layer above it. Opus (materials generation) only ever sees a small,
pre-vetted slice of the original pool.

**Batch API by default.** Prescreening and scoring submit their LLM calls through Anthropic's
Batches API, which is ~50% cheaper than synchronous calls (at the cost of latency — results
may take minutes). Synchronous fallback exists for the UI's on-demand "score/generate now"
buttons and when a batch errors or times out.

**Profile-driven.** One person's preferences live in `profile/` — a YAML of target companies,
dealbreakers, geography, and scoring rubric, plus supporting docs (resume versions, narrative
guidance, voice examples, accomplishments). The prompts template themselves from this profile,
so the pipeline's judgment is entirely configurable without code changes.

**Human-in-the-loop.** Nothing is auto-submitted anywhere. The pipeline produces *drafts and
recommendations*; a person reviews them in the Streamlit UI, tracks application status, and can
override any automated decision (e.g. re-score a job the prescreen rejected).

---

## Data model (SQLite)

A single SQLite database is the shared state between the CLI pipeline and the UI.

| Table | Represents |
|---|---|
| **User** | The profile owner (this build is single-user, but keyed for multi-user). |
| **Job** | One posting: source, company, title, location, URL, JD text, `status` (active / expired / closed). |
| **Score** | An LLM scoring result for a job: tier, sub-scores, gaps, positioning, rationale. |
| **Application** | A generated application: points at the folder of materials on disk. |
| **UserJobMeta** | Per-job/user state: prescreen decision and application tracking status (applied / interviewing / offer / rejected). |

Generated materials are written to disk under `outputs/{user}/{date}_{company}_{role}/`
(`cover_letter.md`, `resume_recommendation.md`, `score.json`, `job.md`), while the DB tracks
where they live and what state each application is in.

---

## The two entry points

Both call the **same** shared code — the stage entry points re-exported from
`src/job_agent/pipeline.py` (implementations in `src/job_agent/stages/`):

- **CLI (`job-agent`)** — the batch path: `job-agent run` does sweep → discover →
  prune closed → score. Generation is separate (`job-agent generate`, or per-job from
  the UI). Plus utilities: `status`, `doctor`, `sync-profile`, `tailor-resume`,
  `batch-fetch`.
- **Streamlit UI** — the operator console: browse and filter jobs, add a job by URL,
  read scores and drafts, trigger single-job score/generate/tailor/cover-letter-Doc
  synchronously, track outcomes, and view pipeline stats.

---

## Housekeeping

Postings don't stay open forever, so two maintenance passes keep the pool honest:

- **Sweep** — flips jobs not re-seen within N days to `expired`.
- **Prune closed** — checks active LinkedIn postings for a "no longer accepting applications"
  notice and flips those to `closed`.

Only `active` jobs flow through scoring, generation, and the UI.

---

## Repo orientation

| Path | What lives there |
|---|---|
| `src/job_agent/pipeline.py` | Re-export shim over `stages/` — the shared stage entry points. |
| `src/job_agent/stages/` | Stage implementations: discover, housekeeping, score, generate, single-job. |
| `src/job_agent/discovery/` | One module per source (LinkedIn, Greenhouse, Lever, email, manual). |
| `src/job_agent/filter.py` | The free rule-based pre-filter. |
| `src/job_agent/clustering.py` | Cluster-key derivation and canonical selection. |
| `src/job_agent/scoring/` | Prescreen (Haiku) + score (Sonnet) prompts and logic. |
| `src/job_agent/generation/` | Cover letter + resume recommendation (Opus). |
| `src/job_agent/batches.py` | Anthropic Batches API submit/poll/collect helpers. |
| `src/job_agent/db.py` | SQLModel schema + session helpers. |
| `app/streamlit_app.py` | The Streamlit operator console (Jobs / Add Job / Stats). |
| `profile/` | The person's config, rubric, and source documents (gitignored). |
| `outputs/` | Generated application materials (gitignored). |
| `flow.mmd` / `architecture.mmd` | The funnel and system-architecture diagrams (Mermaid sources). |
```
