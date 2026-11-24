"""Tests for the resume-recommendation grounding gate: prompt context and
the validate-and-retry loop in generate_resume_recommendation (mocked API).
"""
import job_agent.generation.resume_recommendation as mod
from job_agent.db import Job, Score
from job_agent.generation.resume_recommendation import (
    build_resume_rec_messages,
    generate_resume_recommendation,
)

PROFILE_FILES = {
    "resume_base.md": "Experience: led a 13-user pilot across 4 functions.",
    "accomplishments/starls.md": "STARL: the pilot had 13 users in 4 functions.",
    "accomplishments/brag_sheet.md": "Brag: shipped the pilot.",
}
RESUME = PROFILE_FILES["resume_base.md"]

CLEAN_EDIT = {
    "operation": "replace", "section": "FB Solutions",
    "original": "led a 13-user pilot across 4 functions",
    "suggested": "Ran a 13-user pilot across 4 functions",
    "reason": "verbs",
}
BAD_EDIT = {
    "operation": "replace", "section": "FB Solutions",
    "original": "led a 13-user pilot across 4 functions",
    "suggested": "Achieved 100% adoption across 4 use cases",
    "reason": "impact",
}


def _job() -> Job:
    return Job(id="x", source="manual", company="Acme", title="Data Lead",
               location="Amsterdam", url="https://example.com",
               description_raw="Data Lead role.", status="active")


def _score() -> Score:
    return Score(job_id="x", user_id=1, tier="Strong Fit", skill_fit=85,
                 preference_alignment=80, recommended_resume_version="base",
                 rationale="r", gap_assessment="g", positioning_notes="p",
                 raw_response="")


class _Block:
    def __init__(self, payload, block_id="toolu_test"):
        self.type = "tool_use"
        self.input = payload
        self.id = block_id


class _FakeMessages:
    def __init__(self, payloads):
        self._payloads = list(payloads)
        self.calls = 0
        self.seen_messages = []

    def create(self, **kwargs):
        self.calls += 1
        messages = kwargs["messages"]
        self.seen_messages.append(messages)
        # Model the API's hard rule: an assistant tool_use must be answered
        # by a tool_result block in the immediately following user message.
        # (The real API 400s otherwise; the fake accepted anything, which is
        # how a malformed retry conversation shipped — Nov 24, 2026.)
        for i, msg in enumerate(messages):
            content = msg.get("content")
            if msg.get("role") != "assistant" or not isinstance(content, list):
                continue
            for block in content:
                if getattr(block, "type", None) != "tool_use":
                    continue
                nxt = messages[i + 1] if i + 1 < len(messages) else {}
                blocks = nxt.get("content") if isinstance(nxt.get("content"), list) else []
                assert nxt.get("role") == "user" and any(
                    isinstance(b, dict) and b.get("type") == "tool_result"
                    and b.get("tool_use_id") == block.id for b in blocks), \
                    f"tool_use {block.id} not answered by a tool_result"
        payload = self._payloads.pop(0)
        return type("R", (), {"content": [_Block(payload)]})()


class _FakeClient:
    def __init__(self, payloads):
        self.messages = _FakeMessages(payloads)


def _payload(*edits):
    return {"version_rationale": "strategy", "edits": list(edits)}


def _install(monkeypatch, payloads):
    client = _FakeClient(payloads)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(mod.anthropic, "Anthropic", lambda **kw: client)
    return client


def test_prompt_includes_sources_and_hardening():
    messages = build_resume_rec_messages(
        _job(), _score(), RESUME, {},
        starls_text="STARL STORY", brag_text="BRAG LINE")
    text = messages[0]["content"][0]["text"]
    assert "STARL STORY" in text
    assert "BRAG LINE" in text
    assert "Grounding rules" in text
    assert "Preserve every number and its unit" in text


def test_clean_payload_single_call(monkeypatch):
    client = _install(monkeypatch, [_payload(CLEAN_EDIT)])
    out = generate_resume_recommendation(
        _job(), _score(), RESUME, {}, profile_files=PROFILE_FILES)
    assert client.messages.calls == 1
    assert "Ran a 13-user pilot" in out


def test_violating_payload_retries_and_recovers(monkeypatch):
    client = _install(monkeypatch, [_payload(BAD_EDIT), _payload(CLEAN_EDIT)])
    dropped = []
    out = generate_resume_recommendation(
        _job(), _score(), RESUME, {}, profile_files=PROFILE_FILES,
        dropped=dropped)
    assert client.messages.calls == 2
    assert "100%" not in out
    assert "Ran a 13-user pilot" in out
    assert dropped == []


def test_retry_answers_tool_use_with_tool_result(monkeypatch):
    """Regression (Nov 24, 2026): the corrective retry appended the
    correction as a plain-text user message after the assistant tool_use;
    the API rejects that with a 400 and generation died for any job whose
    first-pass recommendation tripped the claim check."""
    client = _install(monkeypatch, [_payload(BAD_EDIT), _payload(CLEAN_EDIT)])
    generate_resume_recommendation(
        _job(), _score(), RESUME, {}, profile_files=PROFILE_FILES)
    retry_messages = client.messages.seen_messages[1]
    assistant_msg, user_msg = retry_messages[-2], retry_messages[-1]
    assert assistant_msg["role"] == "assistant"
    assert user_msg["role"] == "user"
    (result_block,) = user_msg["content"]
    assert result_block["type"] == "tool_result"
    assert result_block["tool_use_id"] == "toolu_test"
    assert "grounding validation" in result_block["content"]


def test_persistent_violation_drops_edit(monkeypatch):
    client = _install(monkeypatch, [_payload(BAD_EDIT), _payload(BAD_EDIT)])
    dropped = []
    out = generate_resume_recommendation(
        _job(), _score(), RESUME, {}, profile_files=PROFILE_FILES,
        dropped=dropped)
    assert client.messages.calls == 2
    assert "100%" not in out
    assert len(dropped) == 1
    assert "FB Solutions" in dropped[0]
