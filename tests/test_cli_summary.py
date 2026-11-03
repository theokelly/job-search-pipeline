"""The scored summary prints one line per cluster (run + score commands).

run_score returns one (job, score) per scored DB row, including cluster
siblings holding a replicated score; the CLI groups them so a multi-geo
posting doesn't read as if it were scored repeatedly.
"""
from types import SimpleNamespace

from job_agent.cli import _print_scored_summary


def _job(company, title, location=""):
    return SimpleNamespace(company=company, title=title, location=location)


def _score(tier="Strong Fit"):
    return SimpleNamespace(tier=tier, skill_fit=4, preference_alignment=5)


def test_one_line_per_cluster(capsys):
    results = [
        (_job("Jobgether", "Europe Margin Expansion and Analytics Leader", "Amsterdam"), _score()),
        (_job("Jobgether", "Europe Margin Expansion and Analytics Leader", "Berlin"), _score()),
        (_job("Jobgether", "Europe Margin Expansion and Analytics Leader", "Remote"), _score()),
        (_job("N8N", "Internal AI Enablement Lead", "Remote"), _score()),
    ]
    _print_scored_summary(results, indent="  ")
    out = capsys.readouterr().out
    lines = [line for line in out.splitlines() if "→" in line]
    assert len(lines) == 2
    assert "(+2 sibling postings)" in out
    assert "Scored 4 jobs in 2 cluster(s)." in out


def test_singleton_cluster_has_no_note(capsys):
    _print_scored_summary([(_job("N8N", "Internal AI Enablement Lead"), _score())])
    out = capsys.readouterr().out
    assert "sibling" not in out
    assert "Scored 1 jobs in 1 cluster(s)." in out
