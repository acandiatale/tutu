"""The LLM call recorder (`llm-NNNN.json`) creates every record file exclusively (v2.1z-ae): two writers sharing one
record directory (two pods, two threads) never write one number twice and lose no record; a single writer numbers and
writes exactly as before."""
from __future__ import annotations

import errno
import json
import logging
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from sbmig.core.interfaces import ChatMessage, LLMResponse
from sbmig.fix.client import _Recorder

REPO = Path(__file__).resolve().parents[2]


def msgs(text: str) -> list[ChatMessage]:
    return [ChatMessage(role="system", content="sys"), ChatMessage(role="user", content=text)]


def users(d: Path) -> list[str]:
    return [json.loads(p.read_text(encoding="utf-8"))["messages"][1]["content"] for p in sorted(d.glob("llm-*.json"))]


def test_a_single_writer_numbers_and_writes_as_before(tmp_path):
    d = tmp_path / "rec"
    d.mkdir()
    (d / "llm-0002.json").write_text("{}")                   # one file already there: the count starts at 1
    r = _Recorder(d)
    paths = [r.write(msgs(f"call {n}"), LLMResponse(content=f"answer {n}", model="m"), 0.25, role="fix")
             for n in range(3)]
    assert [p.name for p in paths] == ["llm-0003.json", "llm-0004.json", "llm-0005.json"]   # 2 taken → 3 (as before)
    text = paths[0].read_text(encoding="utf-8")
    data = json.loads(text)
    assert text == json.dumps(data, ensure_ascii=False, indent=2)                           # the same bytes as before
    assert (data["response"], data["model"], data["latency"], data["role"]) == ("answer 0", "m", 0.25, "fix")


def test_two_recorders_never_overwrite_a_number_the_other_took(tmp_path, monkeypatch):
    d = tmp_path / "rec"
    a, b = _Recorder(d), _Recorder(d)                        # both counted 0 files: both start at llm-0001
    # an NFS client may still say "no such file" for a record the other pod has just written: exists() is no proof
    monkeypatch.setattr(Path, "exists", lambda self: False)
    pa = a.write(msgs("from a"), None, 0.0, error="x")
    pb = b.write(msgs("from b"), None, 0.0, error="y")
    assert (pa.name, pb.name) == ("llm-0001.json", "llm-0002.json")
    monkeypatch.undo()
    assert users(d) == ["from a", "from b"]


def test_threads_sharing_one_directory_lose_no_record(tmp_path):
    d = tmp_path / "rec"
    writers, per = 4, 25
    recs = [_Recorder(d) for _ in range(writers)]
    start = threading.Barrier(writers)
    names: list[list[str]] = [[] for _ in range(writers)]

    def run(i: int) -> None:
        start.wait(10)
        for n in range(per):
            names[i].append(recs[i].write(msgs(f"{i}-{n}"), None, 0.0).name)
    ts = [threading.Thread(target=run, args=(i,)) for i in range(writers)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(30)
    every = [n for ns in names for n in ns]
    assert len(every) == len(set(every)) == writers * per
    assert sorted(every) == [f"llm-{n:04d}.json" for n in range(1, writers * per + 1)]
    assert sorted(users(d)) == sorted(f"{i}-{n}" for i in range(writers) for n in range(per))


def test_processes_sharing_one_directory_lose_no_record(tmp_path):
    d, go = tmp_path / "rec", tmp_path / "go"
    code = ("import sys, time; from pathlib import Path\n"
            "from sbmig.core.interfaces import ChatMessage\n"
            "from sbmig.fix.client import _Recorder\n"
            "r = _Recorder(Path(sys.argv[1])); go = Path(sys.argv[2])\n"
            "while not go.exists(): time.sleep(0.002)\n"
            "for n in range(20):\n"
            "    r.write([ChatMessage(role='system', content='s'), ChatMessage(role='user', content=f'{sys.argv[3]}-{n}')],"
            " None, 0.0)\n")
    env = {**os.environ, "PYTHONPATH": str(REPO) + os.pathsep + os.environ.get("PYTHONPATH", "")}
    ps = [subprocess.Popen([sys.executable, "-c", code, str(d), str(go), str(i)], cwd=str(REPO), env=env)
          for i in range(3)]
    try:
        go.write_text("go")
        for p in ps:
            assert p.wait(60) == 0
    finally:
        for p in ps:
            if p.poll() is None:
                p.kill()
    assert sorted(p.name for p in d.glob("llm-*.json")) == [f"llm-{n:04d}.json" for n in range(1, 61)]
    assert sorted(users(d)) == sorted(f"{i}-{n}" for i in range(3) for n in range(20))


class _Inner:
    model = "m"

    def __init__(self, fail: bool = False):
        self.fail = fail

    def complete(self, messages, **kw):
        if self.fail:
            raise ValueError("gateway said no")
        return LLMResponse(content="the paid-for answer", model="m")


def _full_disk(monkeypatch, d: Path, *, after_create: bool) -> None:
    """Writing a record fails with ENOSPC (at the create, or after it — a partly written file of ours)."""
    import builtins

    from sbmig.fix import client as clientmod
    real_open = builtins.open

    def full(path, mode="r", *a, **kw):
        if str(path).startswith(str(d)) and "x" in mode:
            if not after_create:
                raise OSError(errno.ENOSPC, "No space left on device")
            fh = real_open(path, mode, *a, **kw)
            fh.write = lambda text: (_ for _ in ()).throw(OSError(errno.ENOSPC, "No space left on device"))
            return fh
        return real_open(path, mode, *a, **kw)
    monkeypatch.setattr(clientmod, "open", full, raising=False)


@pytest.mark.parametrize("after_create", [False, True])
def test_a_record_that_cannot_be_written_never_loses_the_answer(tmp_path, monkeypatch, caplog, after_create):
    from sbmig.fix.client import RecordingClient

    d = tmp_path / "rec"
    rc = RecordingClient(_Inner(), d)
    _full_disk(monkeypatch, d, after_create=after_create)
    with caplog.at_level(logging.WARNING, logger="sbmig"):
        resp = rc.complete(msgs("call"), role="fix")
    assert resp.content == "the paid-for answer"
    assert any("not written" in r.getMessage() and "the call itself is not affected" in r.getMessage()
               for r in caplog.records)
    assert not list(d.glob("llm-*.json"))                    # no half-written record of ours left
    monkeypatch.undo()
    assert _Recorder(d).write(msgs("later"), None, 0.0).name == "llm-0001.json"   # numbering goes on as before


def test_a_failed_record_never_hides_the_calls_own_error(tmp_path, monkeypatch):
    from sbmig.fix.client import RecordingClient

    d = tmp_path / "rec"
    rc = RecordingClient(_Inner(fail=True), d)
    _full_disk(monkeypatch, d, after_create=False)
    with pytest.raises(ValueError, match="gateway said no"):
        rc.complete(msgs("call"))
