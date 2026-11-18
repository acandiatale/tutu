"""Every recorded LLM answer of the replay fixtures (`fixtures/projects/*/replay/llm-*.json`) still parses to what
the parsers accepted before the K1 / K3 / K8 changes: same script text, same edits, same review verdict and issue
count. The golden file `data/replay_parse_golden.json` was written by the parsers of commit f0a2599 (hashes of the
script text and of the edits' model dumps); a new replay file must be added to it."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from sbmig.fix.script_protocol import parse_script_response
from sbmig.review.parse import parse_review_answer

REPO = Path(__file__).resolve().parents[2]
GOLDEN = json.loads((Path(__file__).parent / "data" / "replay_parse_golden.json").read_text(encoding="utf-8"))
FILES = sorted((REPO / "fixtures" / "projects").glob("*/replay/llm-*.json"))


def _h(x) -> str:
    return hashlib.sha256(json.dumps(x, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()[:16]


def test_every_replay_file_is_in_the_golden_file():
    rels = {str(f.relative_to(REPO / "fixtures" / "projects")) for f in FILES}
    assert len(FILES) >= 78 and rels == set(GOLDEN)


@pytest.mark.parametrize("path", FILES, ids=lambda p: str(p.relative_to(REPO / "fixtures" / "projects")))
def test_a_replay_answer_parses_as_before(path: Path):
    entry = json.loads(path.read_text(encoding="utf-8"))
    want = GOLDEN[str(path.relative_to(REPO / "fixtures" / "projects"))]
    assert entry["replay_role"] == want["role"]
    if want["role"] == "fix":
        p = parse_script_response(entry["response"])
        assert _h(p.code) == want["code"] and _h([e.model_dump() for e in p.edits]) == want["edits"]
        assert p.unclosed == [] or not want["usable"]          # nothing usable would read as cut at the token limit
    else:
        p = parse_review_answer(entry["response"], "stop")
        assert p.ok == want["ok"] and len(p.issues) == want["issues"]
        assert _h(p.script) == want["script"] and _h([e.model_dump() for e in p.edits]) == want["edits"]
