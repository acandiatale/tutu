"""`Workspace.snapshot_tree` / `tree_entries` / `make_tree` / `_cat_blobs` (the code-change view's git helpers) on real
temporary git workspaces: HEAD, the real index and `git status` stay as they were."""
from __future__ import annotations

import os

import pytest

from sbmig.core.workspace import GitError, Workspace


@pytest.fixture()
def ws(tmp_path) -> Workspace:
    src = tmp_path / "proj"
    (src / "src/com/x").mkdir(parents=True)
    (src / "pom.xml").write_text("<project/>\n", encoding="utf-8")
    (src / "src/com/x/Foo.java").write_text("class Foo {}\n", encoding="utf-8")
    (src / "lib").mkdir()
    (src / "lib/old.jar").write_bytes(b"PK\x03\x04" + b"\x00" * 30)
    (src / ".gitignore").write_text("*.jar\n*.log\n", encoding="utf-8")
    w = Workspace.from_input(src, tmp_path / "out", run_id="t-trees")
    w.git_init()
    return w


def _state(ws: Workspace) -> tuple[str, bytes, str]:
    index = (ws.root / ".git" / "index").read_bytes()
    return ws.head(), index, ws._git("--no-optional-locks", "status", "--porcelain", "-z", "--untracked-files=all").stdout


def test_snapshot_holds_the_uncommitted_tree_and_leaves_head_index_and_status_alone(ws):
    ws.write("src/com/x/Foo.java", "class Foo { int a; }\n")
    ws.write("src/com/x/New File 한글.java", "class N {}\n")
    ws.delete("pom.xml")
    (ws.root / "lib/new.jar").write_bytes(b"PK\x03\x04new\x00")      # ignored by *.jar: force-added like commit()
    (ws.root / "debug.log").write_text("noise\n", encoding="utf-8")   # ignored, not a jar: left out
    before = _state(ws)
    tree = ws.snapshot_tree()
    assert _state(ws) == before
    entries = ws.tree_entries(tree)
    assert "pom.xml" not in entries and "debug.log" not in entries
    assert {"src/com/x/Foo.java", "src/com/x/New File 한글.java", "lib/old.jar", "lib/new.jar", ".gitignore"} \
        <= set(entries)
    assert entries["src/com/x/Foo.java"][1] != ws.tree_entries(ws.head())["src/com/x/Foo.java"][1]
    assert not [p for p in os.listdir(ws.root / ".git") if p.startswith("sbmig-tmp-")]


def test_snapshot_of_a_clean_tree_is_the_head_tree(ws):
    assert ws.snapshot_tree() == ws._git("rev-parse", "HEAD^{tree}").stdout.strip()


def test_tree_entries_keep_regular_files_only_with_modes(ws):
    os.chmod(ws.root / "pom.xml", 0o755)
    os.symlink("pom.xml", ws.root / "link.xml")
    entries = ws.tree_entries(ws.snapshot_tree())
    assert entries["pom.xml"][0] == "100755" and entries["src/com/x/Foo.java"][0] == "100644"
    assert "link.xml" not in entries


def test_make_tree_round_trips_and_the_empty_tree(ws):
    head = ws.tree_entries(ws.head())
    moved = [(m, s, "moved dir/" + p) for p, (m, s) in head.items()]
    tree = ws.make_tree(moved)
    assert ws.tree_entries(tree) == {"moved dir/" + p: v for p, v in head.items()}
    empty = ws.make_tree([])
    assert ws.tree_entries(empty) == {}
    assert ws._git("cat-file", "-t", empty).stdout.strip() == "tree"
    assert not [p for p in os.listdir(ws.root / ".git") if p.startswith("sbmig-tmp-")]


def test_make_tree_refuses_an_unknown_blob(ws):
    with pytest.raises(GitError):
        ws.make_tree([("100644", "1" * 40, "x.txt")])


def test_cat_blobs_streams_in_order_with_a_size_limit(ws):
    head = ws.tree_entries(ws.head())
    foo, jar = head["src/com/x/Foo.java"][1], head["lib/old.jar"][1]
    got = list(ws._cat_blobs([jar, foo, "2" * 40, foo], limit=20))
    assert [g[0] for g in got] == [jar, foo, "2" * 40, foo]
    assert got[1][1] == b"class Foo {}\n" and got[3][1] == b"class Foo {}\n"
    assert got[0][1] is None and got[0][2] == 34      # over the limit: not kept
    assert got[2][1] is None and got[2][2] == -1                                   # missing
    assert list(ws._cat_blobs([], limit=10)) == []


def test_git_keyword_arguments_leave_plain_calls_alone(ws):
    (ws.root / "abc.txt").write_bytes(b"abc\n")
    assert ws._git("hash-object", "--stdin", input=b"abc\n").stdout.strip() \
        == ws._git("hash-object", "abc.txt").stdout.strip()
    assert ws._git("rev-parse", "--show-toplevel", env={"GIT_DIR": str(ws.root / ".git")}).returncode == 0
    assert ws._git("rev-parse", "--is-inside-work-tree").stdout.strip() == "true"


def test_snapshot_sees_a_same_size_edit_with_the_index_timestamp(ws, monkeypatch):
    # git's racy-clean check compares an entry's mtime with the index file's own mtime: the temporary copy must keep it
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.trustctime")       # ctime cannot be set back: leave it out
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "false")
    foo = ws.root / "src/com/x/Foo.java"
    ws.write("src/com/x/Foo.java", "class Foo {1}\n")
    old = os.stat(foo).st_mtime_ns - 100 * 10**9                        # long before the commit: never smudged
    os.utime(foo, ns=(old, old))
    ws.commit("c1")
    orig = os.stat(foo)
    foo.write_bytes(b"class Foo {2}\n")                                 # same size
    os.utime(foo, ns=(orig.st_atime_ns, orig.st_mtime_ns))               # same mtime as the index entry
    os.utime(ws.root / ".git" / "index", ns=(orig.st_mtime_ns, orig.st_mtime_ns))
    blob = ws.tree_entries(ws.snapshot_tree())["src/com/x/Foo.java"][1]
    assert blob == ws._git("hash-object", "src/com/x/Foo.java").stdout.strip()
