"""Offline tests for coding/agent.py: a scripted fake endpoint stands in for the chat API."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("coding_agent", HERE / "agent.py")
agent = importlib.util.module_from_spec(spec)
sys.modules["coding_agent"] = agent
spec.loader.exec_module(agent)  # type: ignore[union-attr]


# --------------------------------------------------------------------------- helpers
class FakePost:
    """Replies in order; records every request (url, headers, payload)."""

    def __init__(self, replies, status=200, shape="openai"):
        self.replies, self.status, self.shape = list(replies), status, shape
        self.requests: list[tuple[str, dict, dict]] = []

    def __call__(self, url, headers, payload, timeout):
        # snapshot: the agent mutates its messages list after the call
        self.requests.append((url, dict(headers), json.loads(json.dumps(payload))))
        text = self.replies.pop(0) if self.replies else "<done>nothing more</done>"
        if self.shape == "openai":
            body = {"choices": [{"message": {"role": "assistant", "content": text}}]}
        elif self.shape == "custom":
            body = {"data": {"result": {"content": text}}, "need_orgin": True}
        else:
            body = {"content": text}
        return self.status, body


def make_cfg(tmp_path, **kw):
    base = dict(url="https://gw.example/chat", token="tok", model="gpt-6-luna", root=tmp_path, yes=True,
                allow_run=True, quiet=True, max_rounds=6)
    base.update(kw)
    return agent.Config(**base)


def make_agent(tmp_path, replies, **kw):
    post = FakePost(replies)
    a = agent.Agent(make_cfg(tmp_path, **kw), post=post, io_in=lambda _p: "y", io_out=lambda _s: None,
                    session_dir=tmp_path / "_sessions")
    a.ws.backup_dir = tmp_path / "_backups"
    return a, post


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "calc.py").write_text(
        "def add(a, b):\n    return a - b\n\n\ndef sub(a, b):\n    return a - b\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_calc.py").write_text(
        "import sys, pathlib\nsys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))\n"
        "from src.calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    return tmp_path


# --------------------------------------------------------------------------- response extraction
def test_extract_content_shapes():
    ex = agent.extract_content
    assert ex({"choices": [{"message": {"content": "hi"}}]}) == "hi"
    assert ex({"content": "x"}) == "x"
    assert ex({"data": {"content": "y"}}) == "y"
    assert ex({"response": "z"}) == "z"
    blocks = [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]
    assert ex({"choices": [{"message": {"content": blocks}}]}) == "a\nb"
    assert ex("plain text body") == "plain text body"
    assert ex({"data": {"result": {"content": "deep"}}}, "data.result.content") == "deep"
    with pytest.raises(ValueError, match="CODING_RESPONSE_PATH"):
        ex({"weird": {"shape": 1}})


def test_payload_and_headers_match_the_gateway_contract(tmp_path):
    post = FakePost(["pong"])
    client = agent.ChatClient(make_cfg(tmp_path, extra_payload={"temperature": 0}), post=post)
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    assert client.chat(msgs) == "pong"
    url, headers, payload = post.requests[0]
    assert url == "https://gw.example/chat"
    assert headers["Authorization"] == "Bearer tok" and headers["Content-Type"] == "application/json"
    assert payload == {"model": "gpt-6-luna", "need_orgin": True, "messages": msgs, "temperature": 0}


def test_custom_shape_needs_response_path(tmp_path):
    post = FakePost(["ok"], shape="custom")
    client = agent.ChatClient(make_cfg(tmp_path, response_path="data.result.content"), post=post)
    assert client.chat([{"role": "user", "content": "u"}]) == "ok"


def test_http_error_is_retried_then_raised(tmp_path):
    post = FakePost(["a", "b", "c"], status=500)
    client = agent.ChatClient(make_cfg(tmp_path), post=post)
    agent.time.sleep = lambda _s: None
    with pytest.raises(RuntimeError, match="HTTP 500"):
        client.chat([{"role": "user", "content": "u"}])
    assert len(post.requests) == 3


def test_probe_prints_raw_and_extracted(tmp_path, capsys):
    rc = agent.probe(make_cfg(tmp_path), post=FakePost(["pong"]))
    out = capsys.readouterr().out
    assert rc == 0 and "HTTP 200" in out and "extracted reply: 'pong'" in out


# --------------------------------------------------------------------------- protocol parsing
def test_parse_actions_edits_writes_deletes_done_ask():
    text = """Let me look.
<actions>
LIST src
READ src/calc.py
READ src/big.py 10-20
GREP . def add
GREP src/calc.py return
RUN python -m pytest tests -q   # with a comment
</actions>
=== EDIT src/calc.py
<<<<<<< SEARCH
    return a - b
=======
    return a + b
>>>>>>> REPLACE
<<<<<<< SEARCH
def sub
=======
def subtract
>>>>>>> REPLACE
=== WRITE src/new.py
```python
print("hi")
```
=== DELETE src/old.py
<ask>should I also rename callers?</ask>
<done>summary</done>
"""
    p = agent.parse_response(text)
    assert p.lists == ["src"]
    assert p.reads == [("src/calc.py", None), ("src/big.py", (10, 20))]
    assert p.greps == [(".", "def add"), ("src/calc.py", "return")]
    assert p.runs == ["python -m pytest tests -q   # with a comment"]
    assert [(e.path, e.search, e.replace) for e in p.edits] == [
        ("src/calc.py", "    return a - b", "    return a + b"),
        ("src/calc.py", "def sub", "def subtract"),
    ]
    assert p.writes == [("src/new.py", 'print("hi")\n')]
    assert p.deletes == ["src/old.py"]
    assert p.ask == "should I also rename callers?" and p.done == "summary"
    assert agent.parse_response("just prose").is_empty


def test_parse_grep_without_path_searches_root():
    p = agent.parse_response("<actions>\nGREP TODO\n</actions>")
    assert p.greps == [(".", "TODO")]


# --------------------------------------------------------------------------- workspace tools
def test_workspace_confines_paths_and_reads_with_line_numbers(repo):
    ws = agent.Workspace(repo)
    with pytest.raises(ValueError):
        ws.resolve("../outside.txt")
    out = ws.read("src/calc.py", (1, 2))
    assert out.startswith("### src/calc.py (lines 1-2 of 6)") and "    1| def add(a, b):" in out
    assert "ERROR: no such file" in ws.read("nope.py")
    listing = ws.list(".")
    assert "src/calc.py" in listing and ".git" not in listing
    assert "src/calc.py:2:" in ws.grep(".", r"return a - b")
    assert "bad regex" in ws.grep(".", "(")


def test_apply_block_exact_whitespace_tolerant_ambiguous_missing():
    ab = agent.Workspace._apply_block
    assert ab("a\nb\nc\n", "b", "B") == ("a\nB\nc\n", "ok")
    new, note = ab("x = 1  \ny = 2\n", "x = 1\ny = 2", "x = 1\ny = 3")
    assert new == "x = 1\ny = 3\n" and "ignoring trailing whitespace" in note
    assert ab("a\na\n", "a", "b")[0] is None
    assert "not found" in ab("a\n", "zzz", "b")[1]


# --------------------------------------------------------------------------- the loop
def test_task_runs_read_edit_test_done(repo):
    replies = [
        "I need to see the file.\n<actions>\nREAD src/calc.py\n</actions>",
        "Fix:\n=== EDIT src/calc.py\n<<<<<<< SEARCH\ndef add(a, b):\n    return a - b\n=======\n"
        "def add(a, b):\n    return a + b\n>>>>>>> REPLACE\n<actions>\nRUN python -m pytest tests -q\n</actions>",
        "<done>add() fixed, tests pass</done>",
    ]
    a, post = make_agent(repo, replies)
    result = a.run_task("add() returns the wrong value, fix it and run the tests")
    assert result == "add() fixed, tests pass"
    assert (repo / "src" / "calc.py").read_text().count("return a + b") == 1
    assert (repo / "_backups" / "src" / "calc.py").read_text().count("return a - b") == 2   # backup = original
    # the model saw the file, then the diff + test output
    assert "### src/calc.py (lines 1-6 of 6)" in post.requests[1][2]["messages"][-1]["content"]
    third = post.requests[2][2]["messages"][-1]["content"]
    assert "### EDIT src/calc.py\napplied (1 block(s))" in third and "exit code: 0" in third
    assert post.requests[0][2]["messages"][0]["role"] == "system"
    assert "Repository root" in post.requests[0][2]["messages"][1]["content"]
    lines = (repo / "_sessions").glob("*.jsonl")
    assert any(json.loads(ln)["role"] == "assistant" for f in lines for ln in f.read_text().splitlines())


def test_failed_search_is_reported_back_and_rejection_respected(repo):
    replies = [
        "=== EDIT src/calc.py\n<<<<<<< SEARCH\nnot in file\n=======\nx\n>>>>>>> REPLACE",
        "=== EDIT src/calc.py\n<<<<<<< SEARCH\ndef sub\n=======\ndef subtract\n>>>>>>> REPLACE\n<done>d</done>",
    ]
    a, post = make_agent(repo, replies, yes=False)
    a.ask_user = lambda _p: "n"
    assert a.run_task("rename") == "d"
    assert "NOT APPLIED: SEARCH text not found" in post.requests[1][2]["messages"][-1]["content"]
    assert "def sub(" in (repo / "src" / "calc.py").read_text()     # rejected → untouched


def test_ask_stops_and_plain_answer_returns(repo):
    a, _ = make_agent(repo, ["<ask>which file?</ask>"])
    assert a.run_task("do something") == "QUESTION: which file?"
    a, _ = make_agent(repo, ["It is a Python repo with one module."])
    assert a.run_task("what is this?") == "It is a Python repo with one module."


def test_write_delete_and_max_rounds(repo):
    a, _ = make_agent(repo, ["=== WRITE docs/x.md\n```md\n# hi\n```\n=== DELETE src/calc.py\n<done>ok</done>"])
    assert a.run_task("t") == "ok"
    assert (repo / "docs" / "x.md").read_text() == "# hi\n" and not (repo / "src" / "calc.py").exists()
    a, _ = make_agent(repo, ["<actions>\nLIST .\n</actions>"] * 10, max_rounds=3)
    assert "stopped after 3 rounds" in a.run_task("loop")


def test_trim_keeps_system_and_latest(repo):
    a, _ = make_agent(repo, [])
    a.messages += [{"role": "user", "content": "### big\n" + "x" * 100_000},
                   {"role": "assistant", "content": "ok"},
                   {"role": "user", "content": "### big2\n" + "y" * 100_000},
                   {"role": "assistant", "content": "ok2"}]
    a._trim()
    assert a.messages[0]["role"] == "system"
    assert sum(len(m["content"]) for m in a.messages) <= agent.MAX_CONTEXT_CHARS
    assert a.messages[-1]["content"] == "ok2"
