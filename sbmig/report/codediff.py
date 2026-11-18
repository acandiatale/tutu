"""웹 화면의 「코드 변경」에 쓰는 기록을 실행 폴더의 codediff/에 남기는 모듈.
최종 트리의 파일마다 원래 파일(origin)을 찾아 최종 경로 기준으로 비교한다. 위치가 바뀐 파일은 원래 경로도 남긴다.
- git의 이름 변경 감지는 쓰지 않고 origin은 이 모듈이 정한다
- git 프로세스 수는 파일 수와 관계없이 일정하다
- index.json을 마지막에 써서 patch.bin 없이 index.json만 보이는 일이 없게 한다
마무리 단계에서 Orchestrator._finalize_code_diff가 호출한다.

The build side of the code-change view (docs/INTERFACES.md "v2.1z-au"): `<run dir>/codediff/` with `patch.bin`,
`summary.json` and `index.json`. Every regular file of the final tree (the working tree at finalize, through a
temporary index) is compared AT ITS FINAL PATH with its origin, the as-is file (first commit) it came from:
1. the as-is file at the same path;
2. same name + identical bytes, its own path gone from the final tree (longest shared trailing path, then the
   smallest path);
3. same name, gone, not taken: the first of the 5 best candidates by shared trailing path that holds at least
   `SIMILARITY` of the final file's non-blank lines (multiset; never for a binary file);
4. none: added. An as-is file that is gone and nobody's origin is deleted.
A base tree holding each origin's blob at the final path is diffed against the final tree in ONE `git diff`
(`--no-renames --histogram -U<context>`), the deletions in a second one against the empty tree. git's stdout goes
to a raw file, which is scanned once; the per-file hunk ranges are copied into `patch.bin`. Sections are matched to
paths by order through `--numstat -z` on the same two trees (verbatim paths, same order); the counts and every
section's +/- line numbers are checked against it, and a diff whose sections do not agree gives its files no patch
(`omitted: "unavailable"`) instead of a wrong one.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from sbmig.core.workspace import DEFAULT_ENCODING_CANDIDATES, _sniff_bytes

log = logging.getLogger("sbmig.report.codediff")

VERSION = 1
DIR_NAME = "codediff"
MAX_FILES = 20_000                  # entries in index.json; files_total = the number before the cut
SIMILARITY = 0.5                    # rule 3: share of the final file's lines that must occur in the candidate
SIMILAR_TRIES = 5                   # rule 3: candidates tried per final file
MAX_FILE_PATCH = 1 << 20            # one file's hunks over this: omitted "too_large"
MAX_PATCH_TOTAL = 64 << 20          # patch.bin never grows past this: omitted "cap"
MAX_MOVE_GROUPS = 200
CONTENT_LIMIT = 8 << 20             # blobs read for rule 3 / a deleted file's encoding; larger ones are not read
BINARY_SNIFF = 8000                 # git's own rule: a NUL byte in the first 8000 bytes = binary

_DIFF_FLAGS = ("--no-color", "--no-ext-diff", "--no-textconv", "--no-renames", "--histogram", "--no-relative")
# a user's `diff.suppressBlankEmpty=true` would print an empty context line without its leading space
_DIFF_ENV = {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "diff.suppressBlankEmpty", "GIT_CONFIG_VALUE_0": "false"}

Entries = dict[str, tuple[str, str]]        # path -> (mode, sha)


# ------------------------------------------------------------------------------------------ origins
def _parts(path: str) -> list[str]:
    return path.split("/")


def _base(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def _shared_tail(a: list[str], b: list[str]) -> int:
    n = 0
    for x, y in zip(reversed(a), reversed(b)):
        if x != y:
            break
        n += 1
    return n


def _pair_by_tail(finals: list[str], olds: list[str], taken: set[str], origins: dict[str, str]) -> None:
    """Rule 2 within one (file name, blob) group: best-first by the longest shared trailing path, then the final
    path, then the smallest as-is path (the same result as sorting every pair, without building the pairs)."""
    free_f = sorted(f for f in finals if f not in origins)
    free_o = sorted(o for o in olds if o not in taken)
    if not free_f or not free_o:
        return
    fp = {f: _parts(f) for f in free_f}
    op = {o: _parts(o) for o in free_o}
    depth = min(max(len(p) for p in fp.values()), max(len(p) for p in op.values()))
    for k in range(depth, 0, -1):
        by_tail: dict[tuple[str, ...], list[str]] = {}
        for o in free_o:
            if o not in taken and len(op[o]) >= k:
                by_tail.setdefault(tuple(op[o][-k:]), []).append(o)       # free_o is sorted: smallest first
        if not by_tail:
            continue
        for f in free_f:
            if f in origins or len(fp[f]) < k:
                continue
            for o in by_tail.get(tuple(fp[f][-k:]), ()):
                if o not in taken:
                    origins[f] = o
                    taken.add(o)
                    break


def _is_binary(data: bytes) -> bool:
    return b"\0" in data[:BINARY_SNIFF]


def _lines(data: bytes) -> Counter:
    return Counter(s for s in (line.strip() for line in data.split(b"\n")) if s)


def similarity(final: bytes, candidate: bytes) -> float:
    """최종 파일의 빈 줄이 아닌 줄 중에서 후보 파일에도 있는 줄의 비율을 반환한다. 바이너리면 0이다.

    Share of the final file's non-blank lines (multiset, whitespace-trimmed) that occur in `candidate`; 0.0 for an
    empty or binary file."""
    if _is_binary(final) or _is_binary(candidate):
        return 0.0
    fl = _lines(final)
    total = sum(fl.values())
    if not total:
        return 0.0
    cl = _lines(candidate)
    return sum(min(n, cl[line]) for line, n in fl.items()) / total


def compute_origins(asis: Entries, final: Entries,
                    read_blobs: Callable[[list[str]], Iterable[tuple[str, Optional[bytes], int]]],
                    candidates: tuple[str, ...] = ()) -> tuple[dict[str, str], list[str], dict[str, str]]:
    """최종 파일마다 원래 as-is 파일을 정한다. (origin 맵, 삭제된 as-is 경로, 삭제 파일의 인코딩)을 반환한다.

    `({final path: as-is path}, [deleted as-is paths], {deleted path: encoding})` by the four rules of the module
    docstring. `read_blobs(shas)` yields `(sha, bytes | None, size)` (one `git cat-file --batch`); it is called once,
    for the blobs rule 3 compares and the deleted files' encodings need."""
    origins: dict[str, str] = {}
    taken: set[str] = set()
    for f in final:                                         # rule 1
        if f in asis:
            origins[f] = f
            taken.add(f)
    gone = [a for a in asis if a not in final]
    # rule 2: same file name and identical bytes
    groups_o: dict[tuple[str, str], list[str]] = {}
    for a in gone:
        groups_o.setdefault((_base(a), asis[a][1]), []).append(a)
    groups_f: dict[tuple[str, str], list[str]] = {}
    for f in final:
        if f not in origins:
            key = (_base(f), final[f][1])
            if key in groups_o:
                groups_f.setdefault(key, []).append(f)
    for key in sorted(groups_f):
        _pair_by_tail(groups_f[key], groups_o[key], taken, origins)
    # rule 3: same file name, similar content
    left = sorted(a for a in gone if a not in taken)
    by_name: dict[str, list[str]] = {}
    for a in left:
        by_name.setdefault(_base(a), []).append(a)
    tries: list[tuple[int, str, str]] = []
    for f in sorted(final):
        if f in origins or _base(f) not in by_name:
            continue
        fp = _parts(f)
        ranked = sorted(by_name[_base(f)], key=lambda a: (-_shared_tail(fp, _parts(a)), a))[:SIMILAR_TRIES]
        tries += [(_shared_tail(fp, _parts(a)), f, a) for a in ranked]
    need_f = sorted({f for _, f, _ in tries})
    shas = list(dict.fromkeys([final[f][1] for f in need_f] + [asis[a][1] for a in left]))
    content: dict[str, Optional[bytes]] = {}
    enc_of: dict[str, str] = {}
    keep = {final[f][1] for f in need_f} | {asis[a][1] for _, _, a in tries}
    for sha, data, _size in read_blobs(shas):
        if data is not None:
            enc_of[sha] = _sniff_bytes(data, candidates or DEFAULT_ENCODING_CANDIDATES)
            if sha in keep:
                content[sha] = data
    for _score, f, a in sorted(tries, key=lambda t: (-t[0], t[1], t[2])):
        if f in origins or a in taken:
            continue
        fd, ad = content.get(final[f][1]), content.get(asis[a][1])
        if fd is None or ad is None:
            continue
        if similarity(fd, ad) >= SIMILARITY:
            origins[f] = a
            taken.add(a)
    deleted = [a for a in left if a not in taken]
    return origins, deleted, {a: enc_of.get(asis[a][1], "utf-8") for a in deleted}


def move_groups(pairs: Iterable[tuple[str, str]]) -> list[dict[str, Any]]:
    """옮겨진 파일의 (원래 경로, 최종 경로)에서 폴더가 어디로 옮겨졌는지 묶어 파일 수가 많은 순서로 반환한다.

    Folder moves: per `(old_path, path)` the shared trailing components are cut off, what is left is `from` / `to`
    (`""` = the project root, otherwise with a trailing `/`), grouped, most files first (then `from`, `to`)."""
    n: Counter = Counter()
    for old, new in pairs:
        op, np_ = _parts(old), _parts(new)
        k = _shared_tail(op, np_)
        a, b = op[:len(op) - k], np_[:len(np_) - k]
        n[("/".join(a) + "/" if a else "", "/".join(b) + "/" if b else "")] += 1
    return [{"from": f, "to": t, "files": c} for (f, t), c in sorted(n.items(), key=lambda x: (-x[1], x[0]))]


# ------------------------------------------------------------------------------------------ diffs
class _Diff:
    """One `git diff` of two trees: numstat by path and the hunk range of every section in its raw output."""

    def __init__(self, raw: Path):
        self.raw = raw
        self.stat: dict[str, tuple[Optional[int], Optional[int]]] = {}
        self.ranges: dict[str, Optional[tuple[int, int]]] = {}     # path -> (start, length) of its hunks, None
        self.ok = True


def _numstat(blob: bytes) -> list[tuple[str, Optional[int], Optional[int]]]:
    out = []
    for rec in blob.split(b"\0"):
        if not rec:
            continue
        parts = rec.split(b"\t", 2)
        if len(parts) != 3:
            continue
        a, r, path = parts
        out.append((path.decode("utf-8", errors="surrogateescape"),
                    int(a) if a.isdigit() else None, int(r) if r.isdigit() else None))
    return out


def _scan(raw: Path) -> list[tuple[Optional[int], int, int, int]]:
    """Sections of a patch file: (offset of the first `@@ ` line or None, end offset, '+' lines, '-' lines)."""
    sections: list[list] = []
    cur: Optional[list] = None
    off = 0
    with open(raw, "rb") as fh:
        for line in fh:
            if line.startswith(b"diff --git "):
                if cur is not None:
                    cur[1] = off
                    sections.append(cur)
                cur = [None, 0, 0, 0]
            elif cur is not None:
                if cur[0] is None:
                    if line.startswith(b"@@ "):
                        cur[0] = off
                elif line[:1] == b"+":
                    cur[2] += 1
                elif line[:1] == b"-":
                    cur[3] += 1
            off += len(line)
    if cur is not None:
        cur[1] = off
        sections.append(cur)
    return [tuple(s) for s in sections]


def _run_diff(ws: Any, a: str, b: str, context: int, raw: Path) -> _Diff:
    d = _Diff(raw)
    stat = _numstat(ws._git_bytes("diff", *_DIFF_FLAGS, "--numstat", "-z", a, b, env=_DIFF_ENV).stdout or b"")
    with open(raw, "wb") as fh:
        ws._git_bytes("diff", *_DIFF_FLAGS, f"-U{context}", a, b, env=_DIFF_ENV, stdout=fh)
    sections = _scan(raw)
    d.stat = {p: (ad, rm) for p, ad, rm in stat}
    if len(sections) != len(stat):
        d.ok = False
    else:
        for (path, ad, rm), (start, end, plus, minus) in zip(stat, sections):
            if ad is not None and (ad, rm) != (plus, minus):
                d.ok = False
                break
            d.ranges[path] = (start, end - start) if start is not None else None
    if not d.ok:
        log.info("code diff: %d patch sections against %d numstat entries (or the line counts differ): "
                 "the files of this diff get no patch", len(sections), len(stat))
        d.ranges = {}
    return d


# ------------------------------------------------------------------------------------------ the artifact
def _json_path(p: str) -> str:
    return p.encode("utf-8", errors="surrogateescape").decode("utf-8", errors="replace")


def _write_atomic(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _copy_range(src, dst, start: int, length: int) -> None:
    src.seek(start)
    left = length
    while left > 0:
        chunk = src.read(min(left, 1 << 20))
        if not chunk:
            raise OSError("patch section shorter than scanned")
        dst.write(chunk)
        left -= len(chunk)


def _keep(ws: Any, entries: Entries) -> Entries:
    return {p: v for p, v in entries.items() if not ws.is_build_output(p) and not ws.is_ignored(p)}


def _file_is_binary(ws: Any, path: str) -> bool:
    try:
        with open(ws.abs(path), "rb") as fh:
            return _is_binary(fh.read(BINARY_SNIFF))
    except OSError:
        return False


def _remove_artifact(out: Path) -> None:
    shutil.rmtree(out, ignore_errors=True)


def write_code_diff(ws: Any, cfg: Any, *, run_id: Optional[str] = None) -> Optional[dict[str, Any]]:
    """실행 폴더에 codediff/(patch.bin, summary.json, index.json)를 쓰고 summary를 반환한다.
    꺼져 있거나 git 저장소가 없으면 쓰지 않고 None을 반환한다. 실패하면 쓰던 파일을 지우고 예외를 던진다.

    Write `<run dir>/codediff/` and return its summary. `report.code_diff` false or no `<project>/.git`: nothing is
    written, journal `code_diff_skipped`, None. Any failure removes the folder, journals `code_diff_skipped` with
    the error and re-raises (the caller logs one warning). Journal `code_diff` on success."""
    rc = cfg.report
    if not getattr(rc, "code_diff", True):
        ws.journal({"event": "code_diff_skipped", "reason": "report.code_diff is false"})
        return None
    if not (ws.root / ".git").exists():
        ws.journal({"event": "code_diff_skipped", "reason": "the project has no git repository"})
        return None
    t0 = time.monotonic()
    out = ws.run_dir / DIR_NAME
    _remove_artifact(out)
    out.mkdir(parents=True)
    raws = [out / ".diff-1.raw", out / ".diff-2.raw"]
    try:
        summary = _build(ws, cfg, out, raws, run_id or ws.run_id, t0)
    except BaseException as e:
        _remove_artifact(out)
        try:
            ws.journal({"event": "code_diff_skipped", "reason": f"failed: {e}"[:500]})
        except Exception:  # noqa: BLE001 - the journal must not hide the original error
            pass
        raise
    finally:
        for r in raws:
            try:
                r.unlink()
            except OSError:
                pass
    return summary


def _build(ws: Any, cfg: Any, out: Path, raws: list[Path], run_id: str, t0: float) -> dict[str, Any]:
    context = int(getattr(cfg.report, "code_diff_context", 10))
    base_commit = ws.first_commit()
    snapshot = ws.snapshot_tree()
    asis = _keep(ws, ws.tree_entries(base_commit))
    final = _keep(ws, ws.tree_entries(snapshot))
    origins, deleted, deleted_enc = compute_origins(
        asis, final, lambda shas: ws._cat_blobs(shas, CONTENT_LIMIT), tuple(ws.encoding_candidates))

    # one diff for every final file at its final path, one for the deletions
    base_tree = ws.make_tree((final[f][0], asis[origins[f]][1], f) for f in sorted(origins))
    new_tree = ws.make_tree((final[f][0], final[f][1], f) for f in sorted(final))
    d1 = _run_diff(ws, base_tree, new_tree, context, raws[0])
    d2: Optional[_Diff] = None
    if deleted:
        del_tree = ws.make_tree((asis[a][0], asis[a][1], a) for a in deleted)
        d2 = _run_diff(ws, del_tree, ws.make_tree([]), context, raws[1])

    entries: list[dict[str, Any]] = []
    for f in sorted(final):
        a = origins.get(f)
        if a == f and asis[a][1] == final[f][1]:
            continue                                        # unchanged (a mode-only change included)
        e: dict[str, Any] = {"path": f, "old_path": None, "changed": True}
        if a is None:
            e["status"] = "added"
        elif a == f:
            e["status"] = "modified"
        else:
            e.update(status="moved", old_path=a, changed=asis[a][1] != final[f][1])
        e["_diff"] = d1 if e["changed"] else None
        e["encoding"] = ws.encoding_of(f)
        entries.append(e)
    for a in deleted:
        entries.append({"path": a, "old_path": None, "status": "deleted", "changed": True, "_diff": d2,
                        "encoding": deleted_enc.get(a, "utf-8")})
    for e in entries:
        d: Optional[_Diff] = e["_diff"]
        if d is None:                                       # moved only: nothing to diff
            e["binary"] = _file_is_binary(ws, e["path"])
            e["added"] = e["removed"] = 0
            continue
        ad, rm = d.stat.get(e["path"], (0, 0))
        e["binary"] = ad is None
        e["added"], e["removed"] = (ad or 0), (rm or 0)
    entries.sort(key=lambda e: e["path"])

    moves_all = move_groups((e["old_path"], e["path"]) for e in entries if e["status"] == "moved")
    text = [e for e in entries if not e["binary"]]
    summary_counts = {
        "files": len(entries),
        "modified": sum(1 for e in entries if e["status"] == "modified"),
        "added": sum(1 for e in entries if e["status"] == "added"),
        "deleted": sum(1 for e in entries if e["status"] == "deleted"),
        "moved": sum(1 for e in entries if e["status"] == "moved"),
        "moved_only": sum(1 for e in entries if e["status"] == "moved" and not e["changed"]),
        "added_lines": sum(e["added"] for e in text),
        "removed_lines": sum(e["removed"] for e in text),
        "binary": sum(1 for e in entries if e["binary"]),
    }
    files_total = len(entries)
    if files_total > MAX_FILES:                             # changed files before moved-only ones
        entries = sorted(sorted(entries, key=lambda e: (not e["changed"], e["path"]))[:MAX_FILES],
                         key=lambda e: e["path"])

    # patch.bin: the kept hunk ranges, in id order
    patch_path = out / "patch.bin"
    tmp_patch = out / "patch.bin.tmp"
    total = 0
    srcs: dict[int, Any] = {}
    try:
        with open(tmp_patch, "wb") as dst:
            for i, e in enumerate(entries):
                e["id"] = i
                d = e.pop("_diff")
                e["patch"], e["omitted"] = None, ""
                if d is None:
                    continue
                if e["binary"]:
                    e["omitted"] = "binary"
                    continue
                if not d.ok:
                    e["omitted"] = "unavailable"
                    continue
                rng = d.ranges.get(e["path"])
                if rng is None:                             # no hunk: an empty file added or deleted
                    continue
                start, length = rng
                if length > MAX_FILE_PATCH:
                    e["omitted"] = "too_large"
                    continue
                if total + length > MAX_PATCH_TOTAL:
                    e["omitted"] = "cap"
                    continue
                src = srcs.get(id(d))
                if src is None:
                    src = srcs[id(d)] = open(d.raw, "rb")
                _copy_range(src, dst, start, length)
                e["patch"] = [total, length]
                total += length
            dst.flush()
            os.fsync(dst.fileno())
    finally:
        for fh in srcs.values():
            fh.close()
    os.replace(tmp_patch, patch_path)

    files = [{"id": e["id"], "path": _json_path(e["path"]),
              "old_path": _json_path(e["old_path"]) if e["old_path"] else None,
              "status": e["status"], "changed": e["changed"], "binary": e["binary"], "added": e["added"],
              "removed": e["removed"], "encoding": e["encoding"], "patch": e["patch"], "omitted": e["omitted"]}
             for e in entries]
    for m in moves_all:
        m["from"], m["to"] = _json_path(m["from"]), _json_path(m["to"])
    head = {"version": VERSION, "run_id": run_id,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "base": base_commit, "context": context, "zip_root": ws.root.name,
            "summary": summary_counts, "moves": moves_all[:MAX_MOVE_GROUPS], "moves_total": len(moves_all)}
    index = {**head, "files": files, "files_total": files_total}
    summary_doc = {**head, "files_total": files_total}
    _write_atomic(out / "summary.json", json.dumps(summary_doc, ensure_ascii=False, indent=1).encode("utf-8"))
    _write_atomic(out / "index.json", json.dumps(index, ensure_ascii=False, indent=1).encode("utf-8"))   # last
    seconds = round(time.monotonic() - t0, 2)
    ws.journal({"event": "code_diff", "files": summary_counts["files"], "moved": summary_counts["moved"],
                "added_lines": summary_counts["added_lines"], "removed_lines": summary_counts["removed_lines"],
                "seconds": seconds})
    return summary_counts
