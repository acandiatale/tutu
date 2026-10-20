#!/usr/bin/env python3
"""coding/agent.py — a small coding agent over a plain chat-completion endpoint.

The only thing it needs from the model API is `POST(url, headers, json, timeout)` with

    headers = {"Authorization": "Bearer <token>", "Content-Type": "application/json"}
    json    = {"model": "gpt-6-luna", "need_orgin": True,
               "messages": [{"role": "system", "content": ...}, {"role": "user", "content": ...}, ...]}

Everything else (reading files, applying SEARCH/REPLACE edits, running tests) happens locally in this script,
driven by a text protocol the model follows (see prompts/system.md). No tool-calling API is required.

    python coding/agent.py --probe                       # send "hello", print the raw response JSON
    python coding/agent.py "fix the failing test in tests/test_x.py"
    python coding/agent.py                               # interactive chat (multi-turn, same conversation)
    python coding/agent.py --files src/a.py src/b.py "rename foo to bar"   # pre-load files into the context

Configuration: environment variables (or coding/.env, KEY=VALUE lines, never committed):
    CODING_API_URL        full URL of the chat endpoint (required)
    CODING_API_KEY        bearer token (required; falls back to LLM_API_KEY)
    CODING_MODEL          default gpt-6-luna
    CODING_TIMEOUT        seconds, default 180
    CODING_RESPONSE_PATH  dot path to the reply text when auto-detection fails, e.g. choices.0.message.content
    CODING_EXTRA_JSON     extra top-level payload fields as JSON, e.g. {"temperature": 0}
    CODING_NEED_ORIGIN    "true"/"false" — the `need_orgin` payload flag (default true)
    CODING_MAX_ROUNDS     model round-trips per task, default 25
Python 3.10+, standard library only (`requests` is used when installed).
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

HERE = Path(__file__).resolve().parent
SYSTEM_PROMPT_FILE = HERE / "prompts" / "system.md"
IGNORED_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", "target", ".idea", ".mypy_cache",
                ".pytest_cache", "dist", "build", ".backups", "sessions", "out"}
MAX_TOOL_CHARS = 12_000          # per READ / GREP / RUN result sent back to the model
MAX_LIST_ENTRIES = 300
MAX_GREP_HITS = 120
MAX_CONTEXT_CHARS = 160_000      # rough budget for the whole conversation; older tool results are trimmed first
TRIMMED_CHARS = 1_500

PostFn = Callable[[str, dict, dict, float], tuple[int, Any]]   # (url, headers, json, timeout) -> (status, body)


# --------------------------------------------------------------------------- config
def load_dotenv(path: Path) -> None:
    """KEY=VALUE lines → os.environ (existing variables win)."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip("'\""))


@dataclass
class Config:
    url: str = ""
    token: str = ""
    model: str = "gpt-6-luna"
    need_origin: bool = True
    timeout: float = 180.0
    response_path: str = ""
    extra_payload: dict = field(default_factory=dict)
    max_rounds: int = 25
    root: Path = field(default_factory=Path.cwd)
    yes: bool = False               # apply edits without asking
    allow_run: bool = False         # run RUN commands without asking
    quiet: bool = False

    @classmethod
    def from_env(cls, **overrides: Any) -> "Config":
        load_dotenv(HERE / ".env")
        cfg = cls(
            url=os.environ.get("CODING_API_URL", ""),
            token=os.environ.get("CODING_API_KEY") or os.environ.get("LLM_API_KEY", ""),
            model=os.environ.get("CODING_MODEL", "gpt-6-luna"),
            need_origin=os.environ.get("CODING_NEED_ORIGIN", "true").lower() not in ("0", "false", "no"),
            timeout=float(os.environ.get("CODING_TIMEOUT", "180")),
            response_path=os.environ.get("CODING_RESPONSE_PATH", ""),
            extra_payload=json.loads(os.environ.get("CODING_EXTRA_JSON") or "{}"),
            max_rounds=int(os.environ.get("CODING_MAX_ROUNDS", "25")),
        )
        for k, v in overrides.items():
            if v is not None:
                setattr(cfg, k, v)
        return cfg


# --------------------------------------------------------------------------- http
def default_post(url: str, headers: dict, payload: dict, timeout: float) -> tuple[int, Any]:
    """`requests.post(url, headers=..., json=..., timeout=...)` when requests is installed, else urllib."""
    try:
        import requests  # type: ignore

        r = requests.post(url, headers=headers, json=payload, timeout=timeout)
        try:
            body = r.json()
        except ValueError:
            body = r.text
        return r.status_code, body
    except ImportError:
        pass
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            status = resp.status
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        status = e.code
    try:
        return status, json.loads(raw)
    except ValueError:
        return status, raw


def _get_path(data: Any, path: str) -> Any:
    cur = data
    for part in path.split("."):
        if isinstance(cur, list):
            cur = cur[int(part)]
        elif isinstance(cur, dict):
            cur = cur[part]
        else:
            raise KeyError(part)
    return cur


def _as_text(v: Any) -> Optional[str]:
    if isinstance(v, str):
        return v
    if isinstance(v, list):                      # content blocks [{"type": "text", "text": ...}]
        parts = [b.get("text") if isinstance(b, dict) else b for b in v]
        parts = [p for p in parts if isinstance(p, str)]
        return "\n".join(parts) if parts else None
    if isinstance(v, dict) and isinstance(v.get("content"), str):
        return v["content"]
    return None


CANDIDATE_PATHS = (
    "choices.0.message.content", "choices.0.text", "choices.0.delta.content",
    "message.content", "data.choices.0.message.content", "data.content", "data.answer", "data.text",
    "content", "answer", "response", "output", "output_text", "result", "text", "reply", "completion",
)


def extract_content(body: Any, response_path: str = "") -> str:
    """The assistant text inside whatever JSON shape the gateway returns."""
    if isinstance(body, str):
        return body
    if response_path:
        return _as_text(_get_path(body, response_path)) or ""
    for p in CANDIDATE_PATHS:
        try:
            t = _as_text(_get_path(body, p))
        except (KeyError, IndexError, ValueError, TypeError):
            continue
        if t:
            return t
    raise ValueError("cannot find the reply text in the response; set CODING_RESPONSE_PATH. Raw: "
                     + json.dumps(body, ensure_ascii=False)[:1500])


class ChatClient:
    def __init__(self, cfg: Config, post: PostFn = default_post):
        self.cfg, self.post = cfg, post
        self.calls = 0

    def payload(self, messages: list[dict]) -> dict:
        p: dict[str, Any] = {"model": self.cfg.model, "need_orgin": self.cfg.need_origin, "messages": messages}
        p.update(self.cfg.extra_payload)
        return p

    def headers(self) -> dict:
        return {"Authorization": f"Bearer {self.cfg.token}", "Content-Type": "application/json"}

    def raw(self, messages: list[dict]) -> tuple[int, Any]:
        if not self.cfg.url:
            raise RuntimeError("CODING_API_URL is not set")
        if not self.cfg.token:
            raise RuntimeError("CODING_API_KEY is not set")
        self.calls += 1
        return self.post(self.cfg.url, self.headers(), self.payload(messages), self.cfg.timeout)

    def chat(self, messages: list[dict], retries: int = 2) -> str:
        last: Exception | None = None
        for attempt in range(retries + 1):
            try:
                status, body = self.raw(messages)
                if status >= 400:
                    raise RuntimeError(f"HTTP {status}: {json.dumps(body, ensure_ascii=False)[:800]}")
                return extract_content(body, self.cfg.response_path)
            except Exception as e:   # network / gateway hiccups: retry with backoff
                last = e
                if attempt < retries:
                    time.sleep(2 ** attempt)
        raise RuntimeError(f"chat request failed: {last}")


# --------------------------------------------------------------------------- protocol parsing
@dataclass
class Edit:
    path: str
    search: str
    replace: str


@dataclass
class Plan:
    reads: list[tuple[str, Optional[tuple[int, int]]]] = field(default_factory=list)
    lists: list[str] = field(default_factory=list)
    greps: list[tuple[str, str]] = field(default_factory=list)      # (path, regex)
    runs: list[str] = field(default_factory=list)
    edits: list[Edit] = field(default_factory=list)
    writes: list[tuple[str, str]] = field(default_factory=list)
    deletes: list[str] = field(default_factory=list)
    ask: Optional[str] = None
    done: Optional[str] = None

    @property
    def has_inspection(self) -> bool:
        return bool(self.reads or self.lists or self.greps)

    @property
    def has_changes(self) -> bool:
        return bool(self.edits or self.writes or self.deletes)

    @property
    def is_empty(self) -> bool:
        return not (self.has_inspection or self.has_changes or self.runs or self.ask or self.done)


_ACTIONS_RE = re.compile(r"<actions>(.*?)</actions>", re.DOTALL | re.IGNORECASE)
_DONE_RE = re.compile(r"<done>(.*?)</done>", re.DOTALL | re.IGNORECASE)
_ASK_RE = re.compile(r"<ask>(.*?)</ask>", re.DOTALL | re.IGNORECASE)
_FILE_HDR_RE = re.compile(r"^=== (EDIT|WRITE|DELETE) (.+?)\s*$", re.MULTILINE)
_SR_RE = re.compile(r"<<<<<<< SEARCH\n(.*?)\n?=======\n(.*?)\n?>>>>>>> REPLACE", re.DOTALL)
_FENCE_RE = re.compile(r"```[A-Za-z0-9_+\-.]*[ \t]*\n(.*?)\n```", re.DOTALL)
_RANGE_RE = re.compile(r"^(\d+)-(\d+)$")


def _clean_path(p: str) -> str:
    return p.strip().strip("`'\"").rstrip(":")


def parse_response(text: str) -> Plan:
    plan = Plan()
    for block in _ACTIONS_RE.findall(text):
        for raw in block.splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            verb, _, rest = line.partition(" ")
            rest = rest.split("#", 1)[0].strip() if verb.upper() != "RUN" else rest.strip()
            verb = verb.upper()
            if verb == "READ" and rest:
                parts = rest.rsplit(" ", 1)
                rng = None
                if len(parts) == 2 and _RANGE_RE.match(parts[1]):
                    m = _RANGE_RE.match(parts[1])
                    rest, rng = parts[0], (int(m.group(1)), int(m.group(2)))  # type: ignore[union-attr]
                plan.reads.append((_clean_path(rest), rng))
            elif verb == "LIST":
                plan.lists.append(_clean_path(rest or "."))
            elif verb == "GREP" and rest:
                p, _, pat = rest.partition(" ")
                if pat:
                    plan.greps.append((_clean_path(p), pat.strip()))
                else:
                    plan.greps.append((".", p))
            elif verb == "RUN" and rest:
                plan.runs.append(rest)
    m = _DONE_RE.search(text)
    if m:
        plan.done = m.group(1).strip()
    m = _ASK_RE.search(text)
    if m:
        plan.ask = m.group(1).strip()
    headers = list(_FILE_HDR_RE.finditer(text))
    for i, h in enumerate(headers):
        kind, path = h.group(1).upper(), _clean_path(h.group(2))
        body = text[h.end():headers[i + 1].start() if i + 1 < len(headers) else len(text)]
        if kind == "EDIT":
            for s, r in _SR_RE.findall(body):
                plan.edits.append(Edit(path, s, r))
        elif kind == "WRITE":
            fm = _FENCE_RE.search(body)
            content = fm.group(1) if fm else body.strip("\n")
            plan.writes.append((path, content + ("\n" if not content.endswith("\n") else "")))
        elif kind == "DELETE":
            plan.deletes.append(path)
    return plan


# --------------------------------------------------------------------------- local tools
class Workspace:
    def __init__(self, root: Path, backup_dir: Optional[Path] = None):
        self.root = root.resolve()
        self.backup_dir = backup_dir
        self.read_files: set[str] = set()

    def resolve(self, rel: str) -> Path:
        p = (self.root / rel).resolve()
        if p != self.root and self.root not in p.parents:
            raise ValueError(f"path escapes the repository: {rel}")
        return p

    @staticmethod
    def _truncate(text: str, limit: int = MAX_TOOL_CHARS) -> str:
        if len(text) <= limit:
            return text
        return text[:limit] + f"\n…[truncated, {len(text) - limit} more chars; READ a line range for the rest]"

    def read(self, rel: str, rng: Optional[tuple[int, int]] = None) -> str:
        p = self.resolve(rel)
        if not p.is_file():
            return f"ERROR: no such file: {rel}"
        try:
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as e:
            return f"ERROR: cannot read {rel}: {e}"
        self.read_files.add(rel)
        lo, hi = (1, len(lines)) if rng is None else (max(1, rng[0]), min(len(lines), rng[1]))
        body = "\n".join(f"{i:5d}| {lines[i - 1]}" for i in range(lo, hi + 1))
        head = f"### {rel} (lines {lo}-{hi} of {len(lines)})\n"
        return head + self._truncate(body)

    def list(self, rel: str) -> str:
        p = self.resolve(rel)
        if not p.is_dir():
            return f"ERROR: no such directory: {rel}"
        out: list[str] = []
        for dp, dns, fns in os.walk(p):
            dns[:] = sorted(d for d in dns if d not in IGNORED_DIRS and not d.startswith("."))
            base = Path(dp).relative_to(self.root)
            for f in sorted(fns):
                if f.startswith("."):
                    continue
                out.append(str(base / f))
                if len(out) >= MAX_LIST_ENTRIES:
                    out.append(f"…[{MAX_LIST_ENTRIES}+ entries; LIST a subdirectory]")
                    return f"### LIST {rel}\n" + "\n".join(out)
        return f"### LIST {rel}\n" + ("\n".join(out) or "(empty)")

    def grep(self, rel: str, pattern: str) -> str:
        try:
            rx = re.compile(pattern)
        except re.error as e:
            return f"ERROR: bad regex {pattern!r}: {e}"
        p = self.resolve(rel)
        hits: list[str] = []
        files = [p] if p.is_file() else []
        if p.is_dir():
            for dp, dns, fns in os.walk(p):
                dns[:] = sorted(d for d in dns if d not in IGNORED_DIRS and not d.startswith("."))
                files += [Path(dp) / f for f in sorted(fns)]
        for f in files:
            try:
                text = f.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for i, line in enumerate(text.splitlines(), 1):
                if rx.search(line):
                    hits.append(f"{f.relative_to(self.root)}:{i}: {line.strip()[:200]}")
                    if len(hits) >= MAX_GREP_HITS:
                        hits.append(f"…[{MAX_GREP_HITS}+ hits; narrow the pattern or path]")
                        return f"### GREP {rel} {pattern}\n" + "\n".join(hits)
        return f"### GREP {rel} {pattern}\n" + ("\n".join(hits) or "(no matches)")

    # ---- edits
    def _backup(self, rel: str) -> None:
        if self.backup_dir is None:
            return
        src = self.resolve(rel)
        if src.is_file():
            dst = self.backup_dir / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)

    @staticmethod
    def _apply_block(content: str, search: str, replace: str) -> tuple[Optional[str], str]:
        """Exact match first; then a match that ignores trailing whitespace per line. (new content, note)."""
        n = content.count(search)
        if n == 1:
            return content.replace(search, replace, 1), "ok"
        if n > 1:
            return None, f"SEARCH text occurs {n} times; make it unique"
        norm = lambda s: "\n".join(ln.rstrip() for ln in s.splitlines())  # noqa: E731
        c_lines, s_lines = content.splitlines(), norm(search).splitlines()
        if s_lines:
            idxs = [i for i in range(len(c_lines) - len(s_lines) + 1)
                    if [ln.rstrip() for ln in c_lines[i:i + len(s_lines)]] == s_lines]
            if len(idxs) == 1:
                i = idxs[0]
                new_lines = c_lines[:i] + replace.splitlines() + c_lines[i + len(s_lines):]
                trailing = "\n" if content.endswith("\n") else ""
                return "\n".join(new_lines) + trailing, "ok (matched ignoring trailing whitespace)"
            if len(idxs) > 1:
                return None, f"SEARCH text occurs {len(idxs)} times; make it unique"
        return None, "SEARCH text not found; READ the file again and copy the exact lines"

    def preview_edits(self, edits: list[Edit]) -> dict[str, tuple[Optional[str], list[str]]]:
        """path → (new content or None, notes per block)."""
        out: dict[str, tuple[Optional[str], list[str]]] = {}
        by_path: dict[str, list[Edit]] = {}
        for e in edits:
            by_path.setdefault(e.path, []).append(e)
        for path, items in by_path.items():
            notes: list[str] = []
            p = self.resolve(path)
            if not p.is_file():
                out[path] = (None, [f"no such file: {path}"])
                continue
            content = p.read_text(encoding="utf-8", errors="replace")
            ok = True
            for e in items:
                new, note = self._apply_block(content, e.search, e.replace)
                notes.append(note)
                if new is None:
                    ok = False
                    break
                content = new
            out[path] = (content if ok else None, notes)
        return out

    def write(self, rel: str, content: str) -> str:
        p = self.resolve(rel)
        self._backup(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return f"wrote {rel} ({len(content.splitlines())} lines)"

    def delete(self, rel: str) -> str:
        p = self.resolve(rel)
        if not p.is_file():
            return f"ERROR: no such file: {rel}"
        self._backup(rel)
        p.unlink()
        return f"deleted {rel}"

    def diff(self, rel: str, new: str) -> str:
        p = self.resolve(rel)
        old = p.read_text(encoding="utf-8", errors="replace") if p.is_file() else ""
        return "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True),
                                            fromfile=f"a/{rel}", tofile=f"b/{rel}"))

    def run(self, cmd: str, timeout: float = 600) -> str:
        try:
            cp = subprocess.run(cmd, shell=True, cwd=str(self.root), capture_output=True, text=True,
                                timeout=timeout, stdin=subprocess.DEVNULL)
            out = (cp.stdout + cp.stderr).strip()
            return f"### RUN {cmd}\nexit code: {cp.returncode}\n" + self._truncate(out or "(no output)")
        except subprocess.TimeoutExpired:
            return f"### RUN {cmd}\nERROR: timed out after {timeout:.0f}s"


# --------------------------------------------------------------------------- the agent
def repo_context(ws: Workspace) -> str:
    parts = [f"Repository root: {ws.root}"]
    try:
        cp = subprocess.run(["git", "status", "--short", "--branch"], cwd=str(ws.root), capture_output=True,
                            text=True, timeout=20)
        if cp.returncode == 0:
            parts.append("git status --short --branch:\n" + (cp.stdout.strip() or "(clean)"))
    except (OSError, subprocess.TimeoutExpired):
        pass
    top = sorted(x.name + ("/" if x.is_dir() else "") for x in ws.root.iterdir()
                 if x.name not in IGNORED_DIRS and not x.name.startswith("."))
    parts.append("top level: " + ", ".join(top[:80]))
    return "\n".join(parts)


class Agent:
    def __init__(self, cfg: Config, post: PostFn = default_post, io_in: Callable[[str], str] = input,
                 io_out: Callable[[str], None] = print, session_dir: Optional[Path] = None):
        self.cfg = cfg
        self.client = ChatClient(cfg, post)
        self.ask_user, self.say = io_in, io_out
        stamp = time.strftime("%Y%m%d-%H%M%S")
        self.session_dir = session_dir if session_dir is not None else HERE / "sessions"
        self.transcript = self.session_dir / f"{stamp}.jsonl"
        self.ws = Workspace(cfg.root, backup_dir=HERE / ".backups" / stamp)
        self.messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT_FILE.read_text(encoding="utf-8")}]
        self.apply_all = cfg.yes
        self.run_all = cfg.allow_run

    # ---- conversation bookkeeping
    def _add(self, role: str, content: str) -> None:
        self.messages.append({"role": role, "content": content})
        try:
            self.session_dir.mkdir(parents=True, exist_ok=True)
            with open(self.transcript, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"t": time.time(), "role": role, "content": content}, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def _trim(self) -> None:
        """Keep the conversation under MAX_CONTEXT_CHARS: shorten old tool-result messages first, then drop the
        oldest exchanges (never the system prompt or the latest two messages)."""
        total = lambda: sum(len(m["content"]) for m in self.messages)  # noqa: E731
        i = 1
        while total() > MAX_CONTEXT_CHARS and i < len(self.messages) - 2:
            m = self.messages[i]
            if m["role"] == "user" and m["content"].startswith("### ") and len(m["content"]) > TRIMMED_CHARS:
                m["content"] = m["content"][:TRIMMED_CHARS] + "\n…[older tool output trimmed]"
            i += 1
        while total() > MAX_CONTEXT_CHARS and len(self.messages) > 4:
            del self.messages[1:3]

    # ---- confirmations
    def _confirm(self, prompt: str, remember: str) -> bool:
        if getattr(self, remember):
            return True
        while True:
            a = self.ask_user(f"{prompt} [y]es / [n]o / [a]ll / [q]uit: ").strip().lower()
            if a in ("y", "yes"):
                return True
            if a in ("n", "no"):
                return False
            if a in ("a", "all"):
                setattr(self, remember, True)
                return True
            if a in ("q", "quit"):
                raise KeyboardInterrupt

    # ---- executing a plan
    def execute(self, plan: Plan) -> list[str]:
        results: list[str] = []
        for d in plan.lists:
            results.append(self.ws.list(d))
        for path, rng in plan.reads:
            results.append(self.ws.read(path, rng))
        for path, pat in plan.greps:
            results.append(self.ws.grep(path, pat))
        if plan.edits:
            preview = self.ws.preview_edits(plan.edits)
            for path, (new, notes) in preview.items():
                if new is None:
                    results.append(f"### EDIT {path}\nNOT APPLIED: " + "; ".join(notes))
                    continue
                diff = self.ws.diff(path, new)
                self.say(diff if diff else f"(no change for {path})")
                if diff and self._confirm(f"apply edit to {path}?", "apply_all"):
                    self.say(self.ws.write(path, new))
                    results.append(f"### EDIT {path}\napplied ({len(notes)} block(s))\n{diff}")
                else:
                    results.append(f"### EDIT {path}\nREJECTED by the user")
        for path, content in plan.writes:
            diff = self.ws.diff(path, content)
            self.say(diff or f"(write {path}: identical)")
            if self._confirm(f"write {path}?", "apply_all"):
                results.append("### WRITE " + path + "\n" + self.ws.write(path, content))
            else:
                results.append(f"### WRITE {path}\nREJECTED by the user")
        for path in plan.deletes:
            if self._confirm(f"delete {path}?", "apply_all"):
                results.append("### DELETE " + path + "\n" + self.ws.delete(path))
            else:
                results.append(f"### DELETE {path}\nREJECTED by the user")
        for cmd in plan.runs:
            self.say(f"$ {cmd}")
            if self._confirm("run this command?", "run_all"):
                out = self.ws.run(cmd)
                self.say(out)
                results.append(out)
            else:
                results.append(f"### RUN {cmd}\nREJECTED by the user")
        return results

    # ---- one task = N model rounds until <done>, <ask>, or a plain answer
    def run_task(self, task: str, files: list[str] = ()) -> str:
        context = [repo_context(self.ws)]
        for f in files:
            context.append(self.ws.read(f))
        self._add("user", task + "\n\n" + "\n\n".join(context))
        for round_no in range(1, self.cfg.max_rounds + 1):
            self._trim()
            reply = self.client.chat(self.messages)
            self._add("assistant", reply)
            plan = parse_response(reply)
            prose = _ACTIONS_RE.sub("", reply)
            prose = _DONE_RE.sub("", _ASK_RE.sub("", prose))
            prose = _FILE_HDR_RE.split(prose)[0].strip()
            if prose and not self.cfg.quiet:
                self.say(f"\n--- model (round {round_no}) ---\n{prose}\n")
            if plan.is_empty:
                return reply.strip()
            if plan.ask and not plan.has_inspection and not plan.has_changes:
                return f"QUESTION: {plan.ask}"
            results = self.execute(plan)
            if plan.done and not plan.has_inspection:
                return plan.done
            feedback = "\n\n".join(results) if results else "(no actions executed)"
            if plan.ask:
                feedback += "\n\nThe user has not answered your question yet; continue with the results above."
            self._add("user", feedback + "\n\nContinue. When finished, reply with <done>…</done>.")
        return f"stopped after {self.cfg.max_rounds} rounds without <done>; say 'continue' to go on."

    def chat_loop(self) -> None:
        self.say(f"coding agent — model {self.cfg.model}, root {self.ws.root}\n"
                 "commands: /files a b c (pre-load), /yes (auto-apply), /run (auto-run), /reset, /quit")
        files: list[str] = []
        while True:
            try:
                line = self.ask_user("you> ").strip()
            except (EOFError, KeyboardInterrupt):
                self.say("")
                return
            if not line:
                continue
            if line in ("/quit", "/exit", "/q"):
                return
            if line == "/reset":
                self.messages = self.messages[:1]
                self.say("conversation reset")
                continue
            if line.startswith("/files"):
                files = line.split()[1:]
                self.say(f"will pre-load: {files}")
                continue
            if line == "/yes":
                self.apply_all = True
                self.say("edits will be applied without asking")
                continue
            if line == "/run":
                self.run_all = True
                self.say("RUN commands will execute without asking")
                continue
            try:
                answer = self.run_task(line, files)
                files = []
            except KeyboardInterrupt:
                self.say("(interrupted)")
                continue
            except Exception as e:
                self.say(f"ERROR: {e}")
                continue
            self.say(f"\n=== {answer}\n")


# --------------------------------------------------------------------------- cli
def probe(cfg: Config, post: PostFn = default_post) -> int:
    client = ChatClient(cfg, post)
    status, body = client.raw([{"role": "system", "content": "You are a helpful assistant."},
                               {"role": "user", "content": "Reply with the single word: pong"}])
    print(f"HTTP {status}")
    print(json.dumps(body, ensure_ascii=False, indent=2)[:4000] if not isinstance(body, str) else body[:4000])
    try:
        print("\nextracted reply:", repr(extract_content(body, cfg.response_path)))
    except ValueError as e:
        print("\n" + str(e))
        return 1
    return 0 if status < 400 else 1


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="coding agent over a plain chat endpoint (see coding/README.md)")
    ap.add_argument("task", nargs="?", help="what to do; omit for interactive chat")
    ap.add_argument("--root", default=".", help="repository root (default: cwd)")
    ap.add_argument("--files", nargs="*", default=[], help="files to pre-load into the context")
    ap.add_argument("--model", help="override CODING_MODEL")
    ap.add_argument("--url", help="override CODING_API_URL")
    ap.add_argument("--timeout", type=float, help="override CODING_TIMEOUT (seconds)")
    ap.add_argument("--max-rounds", type=int, help="override CODING_MAX_ROUNDS")
    ap.add_argument("--yes", "-y", action="store_true", help="apply edits without asking")
    ap.add_argument("--allow-run", action="store_true", help="run RUN commands without asking")
    ap.add_argument("--quiet", action="store_true", help="do not echo the model's prose")
    ap.add_argument("--probe", action="store_true", help="send a hello request and print the raw response")
    a = ap.parse_args(argv)
    cfg = Config.from_env(url=a.url, model=a.model, timeout=a.timeout, max_rounds=a.max_rounds,
                          root=Path(a.root).resolve(), yes=a.yes or None, allow_run=a.allow_run or None,
                          quiet=a.quiet or None)
    if a.probe:
        return probe(cfg)
    agent = Agent(cfg)
    if a.task:
        print(f"\n=== {agent.run_task(a.task, a.files)}")
        return 0
    agent.chat_loop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
