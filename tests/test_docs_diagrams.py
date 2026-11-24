"""Docs/diagram drift guard (Phase H follow-up).

OVERVIEW.md embeds copies of both Mermaid diagrams — that duplication
is how they drifted from the code in the first place. This test fails
when an embedded copy no longer matches its .mmd source, so a diagram
edit must mirror OVERVIEW in the same commit.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _mermaid_blocks(md_text: str) -> list[str]:
    return [b.strip("\n") for b in re.findall(r"```mermaid\n(.*?)```", md_text, re.S)]


def test_overview_diagram_copies_match_sources():
    blocks = _mermaid_blocks((ROOT / "OVERVIEW.md").read_text())
    assert len(blocks) == 2, "expected the flow + architecture copies in OVERVIEW.md"
    flow = (ROOT / "flow.mmd").read_text().strip("\n")
    architecture = (ROOT / "architecture.mmd").read_text().strip("\n")
    assert blocks[0] == flow, "OVERVIEW's rendered-flow copy drifted from flow.mmd"
    assert blocks[1] == architecture, \
        "OVERVIEW's architecture copy drifted from architecture.mmd"
