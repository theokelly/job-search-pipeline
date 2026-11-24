"""Characterization tests for job_agent.batches (Phase C).

The Anthropic client is faked: submission tests capture the requests the
module builds; collection tests feed canned batch-result entries through
a monkeypatched iter_results. No network, no API key.
"""
from types import SimpleNamespace

import pytest

from job_agent import batches
from job_agent.db import Job


def _job(jid="job1", title="Head of AI"):
    return Job(id=jid, company="Acme", title=title, location="Amsterdam",
               url=f"https://example.com/{jid}", source="test",
               description_raw="A JD. " * 40)


class _FakeBatches:
    def __init__(self):
        self.captured = None

    def create(self, requests):
        self.captured = requests
        return SimpleNamespace(id="batch_123")


@pytest.fixture
def fake_client(monkeypatch):
    fake = _FakeBatches()
    client = SimpleNamespace(messages=SimpleNamespace(batches=fake))
    monkeypatch.setattr(batches, "_client", lambda: client)
    return fake


def _entry(custom_id, result_type="succeeded", blocks=None):
    message = SimpleNamespace(content=blocks or [])
    return SimpleNamespace(
        custom_id=custom_id,
        result=SimpleNamespace(type=result_type, message=message),
    )


def _text_block(text):
    return SimpleNamespace(type="text", text=text)


# ---------------------------------------------------------------------------
# Submission
# ---------------------------------------------------------------------------

def test_submit_prescreen_batch_builds_requests(fake_client):
    jobs = [_job("a1"), _job("b2", "AI Lead")]
    batch_id, by_id = batches.submit_prescreen_batch(jobs, {})
    assert batch_id == "batch_123"
    assert set(by_id) == {"prescreen_a1", "prescreen_b2"}
    assert by_id["prescreen_a1"] is jobs[0]
    reqs = fake_client.captured
    assert [r["custom_id"] for r in reqs] == ["prescreen_a1", "prescreen_b2"]
    assert reqs[0]["params"]["model"] == "claude-haiku-5-5"
    assert reqs[0]["params"]["max_tokens"] == 60
    assert reqs[0]["params"]["messages"][0]["role"] == "user"


def test_submit_score_batch_builds_requests(fake_client, monkeypatch):
    monkeypatch.setattr(batches, "build_scoring_messages",
                        lambda job, profile, narrative: [{"role": "user", "content": "x"}])
    jobs = [_job("a1")]
    batch_id, by_id = batches.submit_score_batch(jobs, {}, "guidance")
    assert batch_id == "batch_123"
    assert set(by_id) == {"score_a1"}
    req = fake_client.captured[0]
    assert req["params"]["model"] == "claude-sonnet-5-5"
    assert req["params"]["max_tokens"] == 1024
    assert req["params"]["tools"] == [batches.SCORE_TOOL]
    assert req["params"]["tool_choice"] == {"type": "auto"}


# ---------------------------------------------------------------------------
# Prescreen collection
# ---------------------------------------------------------------------------

def test_collect_prescreen_results(monkeypatch):
    jobs = {"prescreen_a1": _job("a1"), "prescreen_b2": _job("b2")}
    entries = [
        _entry("prescreen_a1", blocks=[_text_block("APPROVE - fits")]),
        _entry("prescreen_b2", blocks=[_text_block("REJECT - IC role")]),
        _entry("prescreen_unknown", blocks=[_text_block("APPROVE - ghost")]),
    ]
    monkeypatch.setattr(batches, "iter_results", lambda bid: iter(entries))
    out = batches.collect_prescreen_results("batch_123", jobs)
    assert [(j.id, ok) for j, ok, _ in out] == [("a1", True), ("b2", False)]
    assert out[1][2] == "REJECT - IC role"


def test_collect_prescreen_batch_error_becomes_reject(monkeypatch):
    jobs = {"prescreen_a1": _job("a1")}
    entries = [_entry("prescreen_a1", result_type="errored")]
    monkeypatch.setattr(batches, "iter_results", lambda bid: iter(entries))
    out = batches.collect_prescreen_results("batch_123", jobs)
    assert len(out) == 1
    assert out[0][1] is False
    assert "batch error" in out[0][2]


# ---------------------------------------------------------------------------
# Score collection
# ---------------------------------------------------------------------------

_FULL_SCORE = {
    "tier": "Strong Fit", "skill_fit": 80, "preference_alignment": 90,
    "gap_assessment": "g", "positioning_notes": "p", "rationale": "r",
}


def _tool_block(payload):
    return SimpleNamespace(type="tool_use", input=payload)


def test_collect_score_results_happy_path(monkeypatch):
    job = _job("a1")
    entries = [_entry("score_a1", blocks=[_tool_block(dict(_FULL_SCORE))])]
    monkeypatch.setattr(batches, "iter_results", lambda bid: iter(entries))
    out = batches.collect_score_results("batch_123", {"score_a1": job}, user_id=1)
    assert len(out) == 1
    assert out[0][0] is job
    assert out[0][1].tier == "Strong Fit"
    assert out[0][1].skill_fit == 80
    assert out[0][1].preference_alignment == 90


def test_collect_score_results_drops_errored_and_incomplete(monkeypatch):
    job = _job("a1")
    entries = [
        _entry("score_a1", result_type="errored"),
        _entry("score_a1", blocks=[_text_block("no tool call")]),  # no profile -> dropped
        _entry("score_ghost", blocks=[_tool_block(dict(_FULL_SCORE))]),
    ]
    monkeypatch.setattr(batches, "iter_results", lambda bid: iter(entries))
    out = batches.collect_score_results("batch_123", {"score_a1": job}, user_id=1)
    assert out == []


def test_collect_score_results_retries_incomplete_via_sync(monkeypatch):
    job = _job("a1")
    sentinel = object()
    monkeypatch.setattr(batches, "score_job", lambda j, uid, p, n: sentinel)
    entries = [_entry("score_a1", blocks=[_tool_block({"tier": "Skip"})])]  # incomplete payload
    monkeypatch.setattr(batches, "iter_results", lambda bid: iter(entries))
    out = batches.collect_score_results(
        "batch_123", {"score_a1": job}, user_id=1, profile={}, narrative_guidance="")
    assert out == [(job, sentinel)]
