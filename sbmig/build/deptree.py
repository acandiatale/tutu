"""``mvn dependency:tree`` → 어떤 jar 를 누가 classpath 로 끌고 오는가(javax API jar 를 끌고
오는 라이브러리 찾기, 2026-10-22). 왜: classpath 귀속(``sbmig.build.classpath``)은 오류가 말하는 '타입' 의
jar(``javax.persistence-api``)까지만 안다. 아직 javax 를 쓰는 라이브러리(예 ``org.qlrm:qlrm:2.1.1``)는 트리
경로 루트 → qlrm → API 에서 보인다. 흐름상 위치: 최종 빌드 뒤 오케스트레이터가 ``javax_api_inventory`` 를
부른다(``report.javax_api_inventory``:
보고서 "javax API jars still on the classpath" 절, 차단 항목의 ``via``). 핵심 진입점:
``dependency_tree``(``logs/deptree-<모듈>.txt`` 캐시, pom 이 바뀌면 무효), ``parse_tree`` /
``paths_to``, ``javax_api_inventory``.
지킬 것: Maven / IO 실패에도 예외를 내지 않는다(실패하면 [] 를 돌려주고 로그와 저널 ``deptree`` 에 남긴다).

`mvn dependency:tree` → who drags a jar onto the classpath (catalog X1 / X1b, 2026-10-22).

The X1 attribution (``sbmig.build.classpath``) maps the class a javac error names to its jar. For a bare
``incompatible types: jakarta.persistence.Query cannot be converted to javax.persistence.Query`` that is the *type's*
jar (``javax.persistence:javax.persistence-api:2.2``), not the library that still uses javax (``org.qlrm:qlrm:2.1.1``).
The dependency tree knows: ``mvn dependency:tree -Dincludes=javax.persistence:*`` prints the path root → qlrm → API.

* ``dependency_tree(ws, cfg, module, includes)`` → parsed trees (one root per reactor module that printed one),
  cached as ``<run_dir>/logs/deptree-<module>.txt`` and invalidated when the module's (or root) pom changes.
* ``parse_tree(text)`` / ``paths_to(roots, predicate)`` → the plugin's text output as ``TreeNode``s / root→node paths.
* ``javax_api_inventory(ws, cfg, profile)`` → every javax EE API jar still on a module's classpath after the
  migration, with the direct dependency that brings it (``JavaxApiJar``; report section "javax API jars still on the
  classpath", ``BlockedItem.via``).

Nothing here raises for Maven / IO failures: a failed run gives ``[]`` (logged + journal ``deptree``).


"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import logging
import re
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from sbmig.core.config import Config
from sbmig.core.models import GAV, JavaxApiJar
from sbmig.core.workspace import Workspace, logs_dir

from .classpath import _PACKAGE_SKIPS, _has_modules, _module_dir, _pom_stamp, _safe
from .mvn import base_command, mvn_env, run_logged

log = logging.getLogger("sbmig.build.deptree")

DEPTREE_TIMEOUT_S = 10 * 60
DEPENDENCY_PLUGIN = "org.apache.maven.plugins:maven-dependency-plugin:3.8.1"

# javax EE API 의 ``-Dincludes`` 패턴. jakarta.*-api 1.x / 2.x(servlet 4, el 3)는 Jakarta EE 8 재배포라 좌표만
# 새것이고 패키지는 아직 javax.* 다 → 같이 찾는다.
# `-Dincludes` patterns (groupId:artifactId:type:version globs) of the javax EE APIs. The jakarta.*-api 1.x / 2.x (and
# servlet 4 / el 3) releases are the Jakarta EE 8 re-publications: new coordinates, still the javax.* namespace.
JAVAX_API_PATTERNS: tuple[str, ...] = (
    "javax.persistence:*", "org.hibernate.javax.persistence:*", "javax.servlet:*", "javax.validation:*",
    "javax.annotation:*", "javax.inject:*", "javax.transaction:*", "javax.xml.bind:*", "javax.ws.rs:*",
    "javax.jms:*", "javax.mail:*", "com.sun.mail:javax.mail", "javax.websocket:*", "javax.el:*",
    "javax.activation:*", "javax.enterprise:*", "javax.faces:*", "javax.batch:*", "javax.json:*",
    "javax.security.enterprise:*",
    "jakarta.persistence:jakarta.persistence-api:*:2.*", "jakarta.servlet:jakarta.servlet-api:*:4.*",
    "jakarta.validation:jakarta.validation-api:*:2.*", "jakarta.annotation:jakarta.annotation-api:*:1.*",
    "jakarta.xml.bind:jakarta.xml.bind-api:*:2.*", "jakarta.transaction:jakarta.transaction-api:*:1.*",
    "jakarta.inject:jakarta.inject-api:*:1.*", "jakarta.ws.rs:jakarta.ws.rs-api:*:2.*",
    "jakarta.el:jakarta.el-api:*:3.*", "jakarta.mail:jakarta.mail-api:*:1.*",
    "jakarta.activation:jakarta.activation-api:*:1.*",
)


@dataclass
class TreeNode:
    """dependency:tree 의 노드 하나: 좌표, scope, 깊이, 자식, '(omitted for duplicate)' 같은
    메모, 찍힌 그대로의 좌표 텍스트."""
    gav: GAV
    scope: Optional[str] = None
    depth: int = 0
    children: list["TreeNode"] = field(default_factory=list)
    note: str = ""                       # "(omitted for duplicate)", "(version managed from 2.0)", "(optional)" …
    root: bool = False
    text: str = ""                       # the coordinates as printed (g:a:type[:classifier]:version[:scope])

    @property
    def omitted(self) -> bool:
        """중복·충돌로 생략된 노드인가(트리에 찍히지만 실제 classpath 의 그 jar 는 다른 경로로 들어온다)."""
        return "omitted for" in self.note

    def walk(self) -> Iterable["TreeNode"]:
        """자신과 모든 자손을 깊이 우선으로."""
        yield self
        for c in self.children:
            yield from c.walk()


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------
# 가지 줄 '|  +- g:a:jar:1.0:compile' / '\- …': 들여쓰기 3칸이 깊이 1.
_BRANCH_RE = re.compile(r"^(?P<indent>(?:[| ] {2})*)(?:\+- |\\- )(?P<node>.+)$")


def _parse_node(raw: str, depth: int) -> Optional[TreeNode]:
    """노드 텍스트 → TreeNode. 루트는 g:a:packaging[:classifier]:version, 자식은
    g:a:type[:classifier]:version[:scope]. verbose 출력의 '(… - omitted for duplicate)' 괄호
    표기도 읽는다. 못 읽으면 None."""
    s = raw.strip()
    note = ""
    if s.startswith("(") and s.endswith(")"):              # verbose: "(g:a:jar:1.0:compile - omitted for duplicate)"
        inner = s[1:-1]
        coords, _, rest = inner.partition(" - ")
        s, note = coords.strip(), f"({rest.strip()})" if rest else ""
    else:
        m = re.match(r"^(\S+)\s*(.*)$", s)
        if not m:
            return None
        s, note = m.group(1), m.group(2).strip()
    parts = s.split(":")
    scope: Optional[str] = None
    classifier: Optional[str] = None
    if depth == 0:                                         # root: g:a:packaging[:classifier]:version
        if len(parts) == 4:
            g, a, typ, v = parts
        elif len(parts) == 5:
            g, a, typ, classifier, v = parts
        else:
            return None
    else:                                                  # child: g:a:type[:classifier]:version[:scope]
        if len(parts) == 6:
            g, a, typ, classifier, v, scope = parts
        elif len(parts) == 5:
            g, a, typ, v, scope = parts
        elif len(parts) == 4:
            g, a, typ, v = parts
        else:
            return None
    if not g or not a:
        return None
    gav = GAV(group_id=g, artifact_id=a, version=v or None, scope=scope, classifier=classifier or None,
              type=None if typ in ("jar", "") else typ)
    return TreeNode(gav=gav, scope=scope, depth=depth, note=note, root=depth == 0, text=s)


def parse_tree(text: str) -> list[TreeNode]:
    """``dependency:tree`` 텍스트(여러 트리가 이어 붙은 것 포함) → 루트 노드 목록. 못 읽는 줄은 건너뛰고,
    콘솔에서 잡힌 [INFO] / [WARNING] 접두어도 허용한다.

    Text output of `dependency:tree` (one or more appended trees) → root nodes. Unparseable lines are skipped."""
    roots: list[TreeNode] = []
    stack: list[TreeNode] = []
    for line in (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = line.rstrip()
        if not line.strip():
            continue
        line = re.sub(r"^\[(?:INFO|WARNING)\]\s?", "", line)     # tolerate console-captured trees
        m = _BRANCH_RE.match(line)
        if m:
            depth = len(m.group("indent")) // 3 + 1
            node = _parse_node(m.group("node"), depth)
            if node is None or not stack:
                continue
            while stack and stack[-1].depth >= depth:
                stack.pop()
            if not stack:
                continue
            stack[-1].children.append(node)
            stack.append(node)
            continue
        if line[:1] in (" ", "|", "+", "\\"):
            continue
        node = _parse_node(line, 0)
        if node is None:
            continue
        roots.append(node)
        stack = [node]
    return roots


def paths_to(roots: Iterable[TreeNode], predicate: Callable[[TreeNode], bool]) -> list[list[TreeNode]]:
    """``predicate`` 에 맞는 노드까지의 루트 → … → 노드 경로 전부(루트 자신은 매치하지 않음).

    Every root → … → node path whose last node matches `predicate` (roots themselves are never matched)."""
    out: list[list[TreeNode]] = []

    def visit(node: TreeNode, path: list[TreeNode]) -> None:
        for c in node.children:
            p = [*path, c]
            if predicate(c):
                out.append(p)
            visit(c, p)

    for r in roots:
        visit(r, [r])
    return out


# ---------------------------------------------------------------------------
# includes patterns
# ---------------------------------------------------------------------------
def matches_pattern(gav: GAV, pattern: str) -> bool:
    """플러그인의 ``groupId:artifactId:type:version`` glob 비교(뒤쪽 칸이 없으면 아무거나).

    The plugin's `groupId:artifactId:type:version` glob (missing trailing segments = any)."""
    segs = pattern.split(":")
    vals = [gav.group_id, gav.artifact_id, gav.type or "jar", gav.version or ""]
    return all(fnmatch.fnmatchcase(v, p or "*") for v, p in zip(vals, segs))


def is_javax_api(gav: Optional[GAV]) -> bool:
    """좌표가 javax EE API jar(``JAVAX_API_PATTERNS``)인가."""
    return gav is not None and any(matches_pattern(gav, p) for p in JAVAX_API_PATTERNS)


# ---------------------------------------------------------------------------
# running the plugin
# ---------------------------------------------------------------------------
def _includes_suffix(includes: list[str]) -> str:
    if not includes:
        return ""
    return "-" + hashlib.sha1(",".join(sorted(includes)).encode("utf-8")).hexdigest()[:8]


def _run_tree(ws: Workspace, cfg: Config, mdir: str, name: str, out_file: Path, includes: list[str], *,
              package: bool):
    """``dependency:tree`` 한 번(``-DoutputFile`` 에 이어 쓰기, ``-Dincludes`` 로 거르기). 모듈이면
    ``-pl <dir> -am``(위쪽 reactor 모듈의 트리도 함께 찍힌다), ``package=True`` 면 빈 package 와 함께."""
    cmd = base_command(cfg)
    if mdir:
        cmd += ["-pl", mdir, "-am"]          # upstream reactor modules print (append) their trees too
    if package:
        cmd += ["package", *_PACKAGE_SKIPS]
    cmd += [f"{DEPENDENCY_PLUGIN}:tree", f"-DoutputFile={out_file}", "-DappendOutput=true"]
    if includes:
        cmd.append("-Dincludes=" + ",".join(includes))
    cmd.append("-q")
    log_path = ws.new_log_path(f"deptree-{name}{'-pkg' if package else ''}")
    pr = run_logged(cmd, cwd=ws.root, env=mvn_env(cfg), log_path=log_path, timeout_s=DEPTREE_TIMEOUT_S)
    return pr, log_path


def dependency_tree(ws: Workspace, cfg: Config, module: Optional[str] = None,
                    includes: Iterable[str] = ()) -> list[TreeNode]:
    """``module``(디렉토리나 artifactId; None = 루트 = aggregator 면 reactor 전체)의 의존성 트리. 캐시·package
    재시도는 classpath 와 같다(``_PACKAGE_SKIPS``). 절대 예외 없음, 실패면 [].

    Parsed `dependency:tree` of `module` (dir path or artifactId; None = the root project, i.e. the whole reactor
    of an aggregator). Never raises; [] on failure."""
    incl = [i for i in (includes or ()) if i]
    mdir: Optional[str] = None
    try:
        mdir = _module_dir(ws, module)
        if mdir is None:
            log.info("deptree: unknown module %r", module)
            return []
        out_dir = logs_dir(ws)                       # v2.1: logs/deptree-<module>.txt (+ .meta.json), no deptree/
        out_dir.mkdir(parents=True, exist_ok=True)
        name = (_safe(mdir.replace("/", "__")) if mdir else "root") + _includes_suffix(incl)
        out_file = out_dir / f"deptree-{name}.txt"
        meta_file = out_dir / f"deptree-{name}.meta.json"
        stamp = _pom_stamp(ws, mdir)
        if out_file.is_file() and meta_file.is_file():
            try:
                if json.loads(meta_file.read_text(encoding="utf-8")).get("poms") == stamp:
                    return parse_tree(out_file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
        out_file.unlink(missing_ok=True)
        pr, log_path = _run_tree(ws, cfg, mdir, name, out_file, incl, package=False)
        if (pr.exit_code != 0 or not out_file.is_file()) and _has_modules(ws):
            # reactor siblings not resolvable without a package in the same session → package (everything skipped)
            out_file.unlink(missing_ok=True)
            pr, log_path = _run_tree(ws, cfg, mdir, name, out_file, incl, package=True)
        if pr.exit_code != 0 or not out_file.is_file():
            log.warning("deptree: dependency:tree failed for %s (exit %s, see %s)", mdir or "root", pr.exit_code,
                        log_path)
            ws.journal({"event": "deptree", "module": mdir or None, "ok": False, "includes": incl,
                        "log": str(log_path)})
            return []
        meta_file.write_text(json.dumps({"poms": stamp, "includes": incl}), encoding="utf-8")
        roots = parse_tree(out_file.read_text(encoding="utf-8"))
        ws.journal({"event": "deptree", "module": mdir or None, "ok": True, "includes": incl,
                    "log": str(log_path), "roots": len(roots)})
        return roots
    except Exception as exc:                 # never break the caller (report inventory is best effort)
        log.warning("deptree(%s): %s: %s", module, type(exc).__name__, exc)
        return []


# ---------------------------------------------------------------------------
# javax API inventory
# ---------------------------------------------------------------------------
def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _pom_key(ws: Workspace, rel: str) -> Optional[str]:
    """pom 의 ``groupId:artifactId``(groupId 가 없으면 ``<parent>`` 의 것을 물려받음), 못 읽으면 None.

    `groupId:artifactId` of a pom (groupId inherited from <parent> when absent)."""
    try:
        root = ET.fromstring(ws.read(rel))
    except Exception:
        return None
    vals: dict[str, str] = {}
    parent_group = None
    for child in root:
        tag = _local(child.tag)
        if tag in ("groupId", "artifactId"):
            vals[tag] = (child.text or "").strip()
        elif tag == "parent":
            for pc in child:
                if _local(pc.tag) == "groupId":
                    parent_group = (pc.text or "").strip()
    a = vals.get("artifactId")
    g = vals.get("groupId") or parent_group
    return f"{g}:{a}" if a and g else None


def module_dirs(ws: Workspace) -> list[str]:
    """프로젝트 pom 이 있는 모든 디렉토리(루트 '' 가 먼저), ``src/`` 아래 pom 은 뺀다.

    Project-relative directories of every project pom ("" = root first), `src/` poms excluded."""
    out: list[str] = []
    for rel in ws.pom_files():
        if "/src/" in f"/{rel}":
            continue
        d = rel[:-len("pom.xml")].rstrip("/")
        if d not in out:
            out.append(d)
    out.sort(key=lambda d: (d != "", d))
    return out


def _scope_of(node: TreeNode) -> Optional[str]:
    return node.scope or node.gav.scope


def javax_api_inventory(ws: Workspace, cfg: Config, profile: Any = None) -> list[JavaxApiJar]:
    """모듈마다 해결된 classpath 위의 javax EE API jar 와 그것을 끌고 오는 직접 의존성(경로에서 reactor
    형제가 아닌 첫 노드 = ``via``). 루트를 먼저 돌리고(aggregator 면 모든 모듈의 트리가 나온다) 트리가 안 나온
    모듈만 ``-pl <dir> -am`` 으로 따로. test 범위가 아닌 경로를 우선하고 (jar, via) 로 중복 제거, 선언한
    모듈(짧은 경로)이 먼저. 절대 예외 없음.

    javax EE API jars on the resolved classpath of each module, with the direct (non-reactor) dependency that
    drags each in. The root project is run first (on an aggregator that prints every reactor module's tree);
    modules whose tree did not show up get their own `-pl <dir> -am` run. Never raises; [] on failure."""
    try:
        dirs = module_dirs(ws)
        key_to_dir: dict[str, str] = {}
        aid_to_dir: dict[str, str] = {}
        for d in dirs:
            k = _pom_key(ws, f"{d}/pom.xml" if d else "pom.xml")
            if k:
                key_to_dir.setdefault(k, d)
                aid_to_dir.setdefault(k.split(":", 1)[1], d)

        trees: list[tuple[str, TreeNode]] = []          # (module dir, root)
        covered: set[str] = set()

        def collect(roots: list[TreeNode]) -> None:
            for r in roots:
                d = key_to_dir.get(r.gav.key, aid_to_dir.get(r.gav.artifact_id))
                if d is None:
                    d = r.gav.artifact_id
                if d in covered:
                    continue
                covered.add(d)
                trees.append((d, r))

        collect(dependency_tree(ws, cfg, None, JAVAX_API_PATTERNS))
        for d in dirs:
            if d and d not in covered:
                collect(dependency_tree(ws, cfg, d, JAVAX_API_PATTERNS))

        reactor = {r.gav.key for _, r in trees} | set(key_to_dir)
        found: list[tuple[int, JavaxApiJar]] = []
        for d, root in trees:
            paths = [p for p in paths_to([root], lambda n: is_javax_api(n.gav) and not n.omitted)]
            non_test = [p for p in paths if _scope_of(p[-1]) != "test"]
            for p in (non_test or paths):
                leaf = p[-1]
                rest = p[1:-1]
                via_node = next((n for n in rest if n.gav.key not in reactor), None)   # skip reactor siblings
                found.append((len(p), JavaxApiJar(
                    artifact=leaf.gav, module=d or None, scope=_scope_of(leaf),
                    path=[n.text or n.gav.coordinates for n in p],
                    via=via_node.gav if via_node is not None else None,
                    declared=not rest)))
        found.sort(key=lambda t: t[0])                   # the module that declares the library first
        out: list[JavaxApiJar] = []
        seen: set[tuple[str, str]] = set()
        for _, j in found:
            k = (j.artifact.key, j.via.key if j.via else "")
            if k in seen:
                continue
            seen.add(k)
            out.append(j)
        return out
    except Exception as exc:
        log.warning("javax_api_inventory: %s: %s", type(exc).__name__, exc)
        return []
