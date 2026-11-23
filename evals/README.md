# Golden-set evals

Regression tracking for the two LLM judgment stages — prescreen and
scoring — against a small, frozen set of real postings with known
expected outcomes.

## Why

The prescreen once drifted for weeks: verdicts were issued on a
1,000-character JD excerpt that was mostly company boilerplate, so
roles were rejected on title keywords while their actual remit sat
deeper in the text. Nothing in the system noticed. The failure
surfaced only because an independent process disagreed with the
verdicts. This harness exists so that class of failure shows up as a
number that moves, the week it happens.

## The golden set

12 cases, snapshotted from real postings (JD text frozen at admission,
so the set is hermetic — postings expire, cases do not). The mix is
deliberate:

- Roles the prescreen once falsely rejected (expected: approve), most
  with a scoring anchor — a known-good tier and reference subscores.
- One bar-setting case: an internal-enablement role whose JD is all
  enablement delivery (workshops, champions, fluency surveys) with no
  leadership mandate — no team, no direct reports. Initially labeled
  approve on title; relabeled reject on 2026-11-23 after a joint
  review of the JD text. It now guards the leadership bar.
- Clear-cut rejects as controls: a role whose JD states a hard
  language requirement, an out-of-geography hybrid, individual-
  contributor delivery roles, an internship.

Cases live in `golden_cases.json`, which is **gitignored**: it holds
real JD text and company names. `scripts/build_golden_cases.py`
regenerates it from the jobs DB; the builder references postings by id
only. Result files under `results/` are keyed by opaque case slug and
are safe to commit. (Console output names the company and role for
readability; only the file is slug-only.)

## Assertion model

- **Prescreen — exact verdict match.** Cheap and near-deterministic;
  a flip is a real signal. Any miss fails the run (exit 1).
- **Scoring — tier match plus subscore bands** (±15 around the
  reference values recorded at admission). The scorer is not
  deterministic, so exact numbers would flap; band misses are
  reported as warnings, not failures.

## Running

```
.venv/bin/python evals/run_evals.py            # both stages
.venv/bin/python evals/run_evals.py --stage prescreen
```

Costs real API calls (a dozen Haiku prescreens, a handful of Sonnet
scores) — run it on demand, not in CI: after any change to the
prescreen config in the profile YAML, the prompts, truncation caps,
or model versions. The unit suite stays hermetic and free.

## Label drift

Expected outcomes are curated judgments frozen at admission time. As
targeting evolves, labels need re-review — a case that starts failing
may be a regression, or may be the label going stale. Keep the set
small enough that re-labeling is cheap, and record label changes in
the builder when they happen.
