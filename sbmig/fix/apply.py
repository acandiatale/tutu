"""Apply FileChange lists to a Workspace: atomic per file, newline-style preserving, truncation guarded."""
from __future__ import annotations

import difflib
import logging
from dataclasses import dataclass
from typing import Optional

from sbmig.core.models import FileChange
from sbmig.core.workspace import Workspace

from .protocol import normalize_path

log = logging.getLogger("sbmig.fix.apply")

SHRINK_GUARD_RATIO = 0.30          # refuse writes that keep < 30 % of the original size (= dropped > 70 %)
SHRINK_GUARD_MIN_CHARS = 120       # tiny files are exempt from the ratio guard


@dataclass
class BlockFailure:
    path: str
    block: int                      # 1-based index of the modify block for that path (0 = whole-file op)
    reason: str                     # "not_found" | "ambiguous" | other short code
    message: str
    search: Optional[str] = None
    matches: int = 0


class _Overlay:
    """Virtual file system on top of the workspace: path -> text (None = deleted)."""

    def __init__(self, ws: Workspace, base: Optional[dict[str, Optional[str]]] = None):
        self.ws = ws
        self.files: dict[str, Optional[str]] = dict(base or {})

    def exists(self, path: str) -> bool:
        if path in self.files:
            return self.files[path] is not None
        return self.ws.abs(path).is_file()

    def read_raw(self, path: str) -> Optional[str]:
        if path in self.files:
            return self.files[path]
        p = self.ws.abs(path)
        if not p.is_file():
            return None
        return p.read_bytes().decode("utf-8", errors="replace")


def _split_style(text: str) -> tuple[str, str]:
    """Return (text with \\n newlines, newline style)."""
    if "\r\n" in text:
        return text.replace("\r\n", "\n"), "\r\n"
    return text, "\n"


def _norm(s: Optional[str]) -> str:
    return (s or "").replace("\r\n", "\n").replace("\r", "\n")


def _indent(line: str) -> str:
    return line[: len(line) - len(line.lstrip())]


def _reindent(replace: str, search_first: str, file_first: str) -> str:
    """If the model's indentation differs uniformly from the file, shift the replacement accordingly."""
    s_ind, f_ind = _indent(search_first), _indent(file_first)
    if s_ind == f_ind:
        return replace
    lines = replace.split("\n")
    if not all(l.startswith(s_ind) or not l.strip() for l in lines):
        return replace
    return "\n".join((f_ind + l[len(s_ind):]) if l.strip() else l for l in lines)


def apply_block(text: str, search: str, replace: str) -> tuple[Optional[str], str, int]:
    """Apply one search/replace on `text` (all \\n newlines).

    Returns (new_text or None, reason, match_count). reason ∈ {"exact", "whitespace", "not_found", "ambiguous", "empty"}.
    """
    if not search.strip():
        return None, "empty", 0
    count = text.count(search)
    if count == 1:
        idx = text.index(search)
        end = idx + len(search)
        if replace == "" and (idx == 0 or text[idx - 1] == "\n") and text[end:end + 1] == "\n":
            end += 1                     # deleting whole lines: also drop the line break
        return text[:idx] + replace + text[end:], "exact", 1
    if count > 1:
        return None, "ambiguous", count

    # whitespace-normalized line match
    s_lines = search.split("\n")
    while s_lines and not s_lines[0].strip():
        s_lines.pop(0)
    while s_lines and not s_lines[-1].strip():
        s_lines.pop()
    if not s_lines:
        return None, "empty", 0
    f_lines = text.split("\n")
    want = [l.strip() for l in s_lines]
    k = len(want)
    hits = [i for i in range(len(f_lines) - k + 1)
            if f_lines[i].strip() == want[0] and [l.strip() for l in f_lines[i:i + k]] == want]
    if len(hits) > 1:
        return None, "ambiguous", len(hits)
    if not hits:
        return None, "not_found", 0
    i = hits[0]
    rep = _reindent(replace, s_lines[0], f_lines[i])
    if replace == "":
        new_lines = f_lines[:i] + f_lines[i + k:]
    else:
        new_lines = f_lines[:i] + rep.split("\n") + f_lines[i + k:]
    return "\n".join(new_lines), "whitespace", 1


def best_window(text: str, search: str, pad: int = 3) -> tuple[int, int]:
    """1-based inclusive line range of the region of `text` most similar to `search` (for model feedback)."""
    f_lines = text.split("\n")
    s_lines = [l for l in search.split("\n")]
    k = max(1, len(s_lines))
    if not f_lines:
        return 1, 1
    target = "\n".join(l.strip() for l in s_lines)
    best_i, best_r = 0, -1.0
    step = 1 if len(f_lines) <= 4000 else 3
    for i in range(0, max(1, len(f_lines) - k + 1), step):
        cand = "\n".join(l.strip() for l in f_lines[i:i + k])
        sm = difflib.SequenceMatcher(None, target, cand, autojunk=False)
        if sm.real_quick_ratio() <= best_r or sm.quick_ratio() <= best_r:
            continue
        r = sm.ratio()
        if r > best_r:
            best_i, best_r = i, r
    start = max(1, best_i + 1 - pad)
    end = min(len(f_lines), best_i + k + pad)
    return start, end


class PatchApplier:
    def __init__(self, ws: Workspace, allow_overwrite: bool = False):
        self.ws = ws
        self.allow_overwrite = allow_overwrite

    # ------------------------------------------------------------------ public
    def apply(self, changes: list[FileChange]) -> tuple[list[FileChange], list[str]]:
        applied, errors, _, overlay = self._run(changes)
        for path, text in overlay.files.items():
            if text is None:
                if self.ws.exists(path):
                    self.ws.delete(path)
            else:
                p = self.ws.abs(path)
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(text.encode("utf-8"))      # bytes: keep \r\n exactly as computed
        if applied:
            log.info("applied %d change(s) to %d file(s); %d error(s)", len(applied),
                     len({c.path for c in applied}), len(errors))
        return applied, errors

    def dry_run(self, changes: list[FileChange]) -> list[str]:
        return self._run(changes)[1]

    def check(self, changes: list[FileChange]) -> list[BlockFailure]:
        """Structured failures (used by the fixer's feedback round)."""
        return self._run(changes)[2]

    # ------------------------------------------------------------------ core
    def _run(self, changes: list[FileChange]):
        overlay = _Overlay(self.ws)
        applied: list[FileChange] = []
        errors: list[str] = []
        failures: list[BlockFailure] = []

        groups: dict[str, list[FileChange]] = {}
        for ch in changes:
            path, warn = normalize_path(ch.path)
            if warn or not path:
                errors.append(f"{ch.path}: {warn}")
                failures.append(BlockFailure(ch.path, 0, "bad_path", str(warn)))
                continue
            new_path = None
            if ch.op == "rename":
                new_path, warn = normalize_path(ch.new_path or "")
                if warn or not new_path:
                    errors.append(f"{path}: rename target {warn or 'missing'}")
                    failures.append(BlockFailure(path, 0, "bad_path", f"rename target {warn or 'missing'}"))
                    continue
            groups.setdefault(path, []).append(ch.model_copy(update={"path": path, "new_path": new_path}))

        for path, group in groups.items():
            g_overlay = _Overlay(self.ws, overlay.files)
            g_errors, g_failures = self._apply_group(path, group, g_overlay)
            if g_errors:
                errors.extend(g_errors)
                n_mod = sum(1 for c in group if c.op == "modify")
                if len(group) > 1:
                    errors.append(f"{path}: none of the {len(group)} change(s) applied ({n_mod} modify block(s); atomic per file)")
                failures.extend(g_failures)
                continue
            overlay.files = g_overlay.files
            applied.extend(group)
        return applied, errors, failures, overlay

    def _apply_group(self, path: str, group: list[FileChange], ov: _Overlay) -> tuple[list[str], list[BlockFailure]]:
        errors: list[str] = []
        failures: list[BlockFailure] = []
        raw_orig = ov.read_raw(path)
        cur_path = path
        content: Optional[str] = None
        style = "\n"
        if raw_orig is not None:
            content, style = _split_style(raw_orig)
        orig_len = len(content) if content is not None else 0
        block_no = 0
        deleted = False

        for ch in group:
            if ch.op == "modify":
                block_no += 1
                if content is None:
                    if not (ch.search or "").strip():
                        # empty SEARCH on a missing file = create
                        content = _norm(ch.replace)
                        if content and not content.endswith("\n"):
                            content += "\n"
                        continue
                    msg = f"{path} block {block_no}: file does not exist"
                    errors.append(msg)
                    failures.append(BlockFailure(path, block_no, "missing_file", msg, ch.search))
                    continue
                new, reason, count = apply_block(content, _norm(ch.search), _norm(ch.replace))
                if new is None:
                    if reason == "ambiguous":
                        msg = f"{path} block {block_no}: search text matches {count} times (ambiguous)"
                    elif reason == "empty":
                        msg = f"{path} block {block_no}: search text is empty"
                    else:
                        msg = f"{path} block {block_no}: search text not found"
                    errors.append(msg)
                    failures.append(BlockFailure(path, block_no, reason, msg, ch.search, count))
                    continue
                if reason == "whitespace":
                    log.debug("%s block %d matched after whitespace normalization", path, block_no)
                content = new
            elif ch.op == "create":
                body = _norm(ch.content)
                if body and not body.endswith("\n"):
                    body += "\n"
                if content is not None and not deleted:
                    if content == body:
                        continue                                  # identical: no-op
                    if not self.allow_overwrite:
                        msg = f"{path}: create refused, file already exists (use MODIFY)"
                        errors.append(msg)
                        failures.append(BlockFailure(path, 0, "exists", msg))
                        continue
                content, deleted = body, False
            elif ch.op == "delete":
                if content is None or deleted:
                    msg = f"{path}: delete failed, file does not exist"
                    errors.append(msg)
                    failures.append(BlockFailure(path, 0, "missing_file", msg))
                    continue
                deleted = True
            elif ch.op == "rename":
                if content is None or deleted:
                    msg = f"{path}: rename failed, file does not exist"
                    errors.append(msg)
                    failures.append(BlockFailure(path, 0, "missing_file", msg))
                    continue
                if ov.exists(ch.new_path or ""):
                    msg = f"{path}: rename failed, target {ch.new_path} already exists"
                    errors.append(msg)
                    failures.append(BlockFailure(path, 0, "exists", msg))
                    continue
                cur_path = ch.new_path or path

        if errors:
            return errors, failures

        if deleted:
            ov.files[path] = None
            return [], []

        if content is None:
            return [], []

        # sanity guard against truncated model output
        if path.endswith(".java") and not content.strip():
            msg = f"{path}: refused to write an empty Java file (suspected truncated output)"
            return [msg], [BlockFailure(path, 0, "truncated", msg)]
        if raw_orig is not None and orig_len >= SHRINK_GUARD_MIN_CHARS and len(content) < orig_len * SHRINK_GUARD_RATIO:
            drop = 100 - int(100 * len(content) / max(1, orig_len))
            msg = f"{path}: refused, file would shrink by {drop}% (suspected truncated output; use DELETE to remove files)"
            return [msg], [BlockFailure(path, 0, "truncated", msg)]

        if style == "\r\n":
            content = content.replace("\n", "\r\n")
        if cur_path != path:
            ov.files[path] = None
        ov.files[cur_path] = content
        return [], []
