"""필터링되는 리소스 아래 바이너리 파일 규칙(v2.1m) — ``<filtering>true</filtering>`` 리소스에 든 바이너리 파일
(웹폰트, 압축 파일, 회사 확장자 ``.dmdb`` …)을 maven-resources-plugin 이 텍스트로 읽다 ``MalformedInputException:
Input length = 1`` 로 실패하는 것을 LLM 없이 막는다. 흐름상 위치: 재구성(restructurer.py ``_non_filtered_extensions``)
이 모듈 pom 이 확정된 뒤 미리 적고, 수정 루프의 결정적 시도(``reactive_targets`` → ``binary_resource_plan(...,
errors=...)``)가 빌드 오류를 보고 다시 적는다. 핵심 진입점: ``binary_resource_plan`` (무엇을 어느 pom 에),
``apply_non_filtered_extensions`` (PomEditor 로 적기, 텍스트 수술 없음), ``reactive_targets``. 지킬 것: 필터링은
끄지 않는다(``@project.version@`` 이 계속 풀려야 함), 파일은 지우지 않는다, 플러그인 기본값(jpg jpeg gif bmp png)과
텍스트 확장자는 적지 않는다, 예외를 내지 않는다(DEBUG 로그 + 빈 결과), 두 번 돌려도 바뀌는 것이 없다(멱등).

Binary files under Maven resource filtering (v2.1m, docs/INTERFACES.md "v2.1m"): maven-resources-plugin ≥ 3.2 reads
every file of a `<filtering>true</filtering>` resource as text, so webfonts, archives and company formats (`.dmdb`)
fail the copy with `MalformedInputException: Input length = 1` (`filtering <f> to <t> failed with …`). v1 fixed it
with handler E12 `resources-binary-filtering` (regex surgery, deleted in v2 step 4); v2.1m puts the rule back,
deterministically, through `PomEditor`:

* `binary_resource_plan(ws, cfg, poms)` — for every pom whose plugin configuration reaches a filtered resource, the
  extensions to add (the analyzer's scan `sbmig.analyze.properties.filtered_binary_resources` on the CURRENT tree +
  `cfg.build.non_filtered_extensions`), minus what the plugin already lists; `errors=` restricts it to the poms
  `reactive_targets` names and adds the extension of the file each error names.
* `apply_non_filtered_extensions(ws, plan)` — writes `<configuration><nonFilteredFileExtensions>` (plugin level only)
  on maven-resources-plugin (and maven-war-plugin for filtered `<webResources>`). Idempotent.
* `reactive_targets(ws, errors)` — the poms to edit for a resources / war plugin filtering failure.

Pure functions; they never raise (DEBUG log, empty result)."""
from __future__ import annotations

import logging
import posixpath
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from lxml import etree
from pydantic import BaseModel, Field

from sbmig.analyze.properties import (
    PLUGIN_UNFILTERED_DEFAULTS,
    TEXT_RESOURCE_EXTENSIONS,
    filtered_binary_resources,
)
from sbmig.core.models import PluginInfo

from .pom_editor import DEFAULT_PLUGIN_GROUP, PomEditError, PomEditor

log = logging.getLogger("sbmig.restructure.resources")

RESOURCES_PLUGIN = "maven-resources-plugin"
WAR_PLUGIN = "maven-war-plugin"
PLUGIN_ORDER = (RESOURCES_PLUGIN, WAR_PLUGIN)
# the wording of the failure (knowledge/notes/maven-plugins.md, v1 handler E12 triggers)
REACTIVE_RE = re.compile(r"MalformedInputException|UnmappableCharacterException|Input length = \d+|"
                         r"filtering \S.* to \S.* failed with")
_FILE_RE = re.compile(r"filtering\s+(?P<path>\S(?:.*?\S)?)\s+to\s+\S")
_PROJECT_RE = re.compile(r"on project (?P<aid>[\w.\-]+)")
# 이 오류가 정말 리소스 / war 플러그인의 복사에서 났는지: javac 의 인코딩 오류 같은 다른 실패에 손대지 않게
_PLUGIN_HINT_RE = re.compile(r"maven-resources-plugin|maven-war-plugin|\b(?:resources|war):[\w-]+\b|"
                             r"filtering \S.* to \S")
_WAR_HINT_RE = re.compile(r"maven-war-plugin|\bwar:(?:war|exploded|inplace)\b")
_EXT_RE = re.compile(r"[a-z0-9][a-z0-9_+\-]{0,15}")
# Maven 상속: 부모 <build><resources> 는 자식이 자기 <resources> 를 선언하지 않을 때만 물려받는다
_DEFAULT_FILTERED_POM = ("<project><build><resources><resource><directory>src/main/resources</directory>"
                         "<filtering>true</filtering></resource></resources></build></project>")
APPEND_ATTR = "combine.children"
MAX_FOLLOWUP_FILES = 10


class ResourcePlan(BaseModel):
    """적을 내용: pom 마다 추가할 확장자(``extensions_by_pom``)와 그 플러그인(``plugins_by_pom``), 아직 덮이지 않은
    바이너리 파일(``files`` — 확장자 없는 것 포함), 사람이 할 일(``followups``: 확장자 없는 파일). 비어 있으면 할 일 없음.

    What `apply_non_filtered_extensions` writes: per pom the missing extensions (lower-case, no dot, sorted) and
    the plugins they go to (maven-resources-plugin first); `files` = the filtered binary files no
    `<nonFilteredFileExtension>` covers yet (this plan's and the extension-less ones — what the structure review
    reports as left); `followups` = extension-less binaries `nonFilteredFileExtensions` cannot cover."""
    extensions_by_pom: dict[str, list[str]] = Field(default_factory=dict)
    plugins_by_pom: dict[str, list[str]] = Field(default_factory=dict)
    files: list[str] = Field(default_factory=list)
    followups: list[str] = Field(default_factory=list)

    @property
    def empty(self) -> bool:
        """추가할 확장자가 없는가(후속 작업은 따로 본다)."""
        return not any(self.extensions_by_pom.values())

    def lines(self, only: Iterable[str] | None = None) -> list[str]:
        """보고 줄 ``<pom>: ext1, ext2 → maven-resources-plugin[, maven-war-plugin]`` (``only`` = 실제로 바뀐 pom 만).

        `RestructureResult.non_filtered_extensions` lines, optionally only for the poms in `only`."""
        keep = set(only) if only is not None else None
        out: list[str] = []
        for pom, exts in self.extensions_by_pom.items():
            if not exts or (keep is not None and pom not in keep):
                continue
            plugins = self.plugins_by_pom.get(pom) or [RESOURCES_PLUGIN]
            out.append(f"{pom}: {', '.join(exts)} → {', '.join(plugins)}")
        return out


# ---------------------------------------------------------------------------------------------- tree facts
@dataclass
class _Facts:
    """pom 하나에 대해 계획이 알아야 할 것(파싱 한 번)."""
    path: str
    ed: PomEditor
    tree_parent: str | None          # the in-tree <parent> pom (relativePath / ../pom.xml, artifactId match)
    has_parent: bool                    # any <parent> at all
    filters_resources: bool             # <filtering>true under resources / testResources (build, profile, copy-resources)
    filters_web: bool                   # <filtering>true under maven-war-plugin <webResources>
    own_resources: bool                 # declares <build><resources> (then nothing of the parent's is inherited)

    @property
    def filters(self) -> bool:
        return self.filters_resources or self.filters_web


@dataclass
class _Want:
    """대상 pom 하나에 적고 싶은 것(중복 제거 전)."""
    plugins: set[str] = field(default_factory=set)
    exts: set[str] = field(default_factory=set)
    files: dict[str, str] = field(default_factory=dict)       # file → its extension ("" = none usable)


class _View:
    """``filtered_binary_resources`` 에 넘기는 작업공간 보기: 트리 목록(glob)을 한 번만 읽고, pom 하나의 텍스트를
    다른 텍스트로 바꿔 보일 수 있다(부모에게서 물려받은 <resources> 로 자식 모듈을 훑을 때)."""

    def __init__(self, ws: Any, overrides: dict[str, str] | None = None,
                 cache: dict[str, list[str]] | None = None):
        self._ws = ws
        self._over = dict(overrides or {})
        self._cache = cache if cache is not None else {}

    def with_text(self, pom: str, text: str) -> _View:
        return _View(self._ws, {**self._over, _norm(pom): text}, self._cache)

    def read(self, rel: Any, *args: Any, **kwargs: Any) -> str:
        key = _norm(str(rel))
        if key in self._over:
            return self._over[key]
        return self._ws.read(rel, *args, **kwargs)

    def glob(self, pattern: str) -> list[str]:
        if pattern not in self._cache:
            self._cache[pattern] = list(self._ws.glob(pattern))
        return self._cache[pattern]

    def abs(self, rel: Any) -> Any:
        return self._ws.abs(rel)


def _norm(path: str) -> str:
    p = posixpath.normpath(str(path).replace("\\", "/"))
    return "" if p == "." else p


def _ext(name: str) -> str:
    """Maven 과 같은 확장자(마지막 점 뒤, 소문자): ``.items.dmdb`` → ``dmdb``, 점이 없으면 ''."""
    base = posixpath.basename(str(name).replace("\\", "/"))
    return base.rsplit(".", 1)[1].lower() if "." in base else ""


def _usable(ext: str) -> bool:
    """적어도 되는 확장자인가: 모양이 맞고, 플러그인 기본값도 텍스트 확장자도 아닌 것."""
    return bool(_EXT_RE.fullmatch(ext)) and ext not in PLUGIN_UNFILTERED_DEFAULTS \
        and ext not in TEXT_RESOURCE_EXTENSIONS


def _company_exts(cfg: Any) -> list[str]:
    """``build.non_filtered_extensions`` (회사 확장자, 예: dmdb) — 소문자, 점 없이, 쓸 수 있는 것만."""
    raw = getattr(getattr(cfg, "build", None), "non_filtered_extensions", None) or []
    if isinstance(raw, str):
        raw = [raw]
    out: list[str] = []
    for x in raw:
        e = str(x).strip().lower().lstrip(".")
        if _usable(e):
            if e not in out:
                out.append(e)
        elif e:
            log.debug("build.non_filtered_extensions: %r ignored (image default / text extension / not an extension)", x)
    return out


def _pom_paths(ws: Any, poms: Iterable[Any] | None) -> list[str]:
    if poms is None:
        items: list[Any] = [p for p in ws.pom_files() if "/src/" not in f"/{p}"]
    else:
        items = list(poms)
    out: list[str] = []
    for p in items:
        rel = _norm(p if isinstance(p, str) else getattr(p, "path", "") or "")
        if rel and rel not in out:
            out.append(rel)
    return out


def _tree_parent(ws: Any, path: str, ed: PomEditor) -> str | None:
    """저장소 안 <parent> pom (restructurer ``_chain`` 과 같은 규칙: 빈 relativePath 면 없음, artifactId 가 맞아야 함)."""
    parent = ed.get_parent()
    if parent is None:
        return None
    rp = ed.parent_relative_path()
    if rp == "":
        return None
    target = _norm(posixpath.join(posixpath.dirname(path), rp or "../pom.xml"))
    if not target.endswith(".xml"):
        target = _norm(posixpath.join(target, "pom.xml"))
    if not target or target.startswith("..") or target == path:
        return None
    try:
        if not ws.exists(target):
            return None
        if PomEditor(ws.abs(target)).artifact_id() != parent.artifact_id:
            return None
    except (PomEditError, OSError):
        return None
    return target


def _filter_kinds(ed: PomEditor) -> tuple[bool, bool]:
    """(resources / testResources 필터링, webResources 필터링). 주석은 lxml 이 요소로 보지 않는다."""
    res = web = False
    for el in ed.root.iter():
        if not isinstance(el.tag, str) or PomEditor.local_name(el) != "filtering":
            continue
        if (el.text or "").strip().lower() != "true":
            continue
        names = {PomEditor.local_name(a) for a in el.iterancestors() if isinstance(a.tag, str)}
        if "webResources" in names:
            web = True
        elif names & {"resources", "testResources"}:
            res = True
    return res, web


def _load_facts(ws: Any, paths: list[str]) -> dict[str, _Facts]:
    out: dict[str, _Facts] = {}
    for p in paths:
        try:
            if not ws.exists(p):
                continue
            ed = PomEditor(ws.abs(p))
        except (PomEditError, OSError) as e:
            log.debug("resources filtering: skipped unparsable pom %s: %s", p, e)
            continue
        res, web = _filter_kinds(ed)
        out[p] = _Facts(path=p, ed=ed, tree_parent=_tree_parent(ws, p, ed), has_parent=ed.get_parent() is not None,
                        filters_resources=res, filters_web=web, own_resources=ed.find("build/resources") is not None)
    return out


# ---------------------------------------------------------------------------------------------- plugin element
def _chosen_plugin(ed: PomEditor, artifact: str, in_tree_parent: bool) -> Any:
    """고칠 플러그인 요소(결정표): <build><plugins> 선언이 있으면 그것, 없고 pom 이 바깥 부모(역할 부모 comp-jar /
    boot-jar 등)의 모듈이 아니면 <pluginManagement> 선언, 그 밖은 None(새로 만든다).

    The element to extend: a `<build><plugins>` declaration wins (its configuration merges over the managed one);
    else a `<pluginManagement>` one when the pom has no parent or an in-tree one — on a parent outside the tree (a
    role parent) an inherited `<build><plugins>` declaration would win over this pom's management entry; else None."""
    built = ed.find_plugins(DEFAULT_PLUGIN_GROUP, artifact, "build")
    if built:
        return built[0]
    if in_tree_parent:
        managed = ed.find_plugins(DEFAULT_PLUGIN_GROUP, artifact, "management")
        if managed:
            return managed[0]
    return None


def _entries(el: Any) -> list[str]:
    """플러그인 수준 ``<configuration><nonFilteredFileExtensions>`` 의 항목(소문자, 점 없이). 실행(execution)의
    configuration 은 보지 않는다."""
    if el is None:
        return []
    nfe = PomEditor.child(PomEditor.child(el, "configuration"), "nonFilteredFileExtensions")
    return [(x.text or "").strip().lower().lstrip(".") for x in PomEditor.children(nfe, "nonFilteredFileExtension")]


def _has_own_list(ed: PomEditor, artifact: str) -> bool:
    """이 pom 이 그 플러그인의 플러그인 수준 목록을 직접 갖는가 — 갖고 있으면 부모 목록을 덮어쓰므로(Maven 설정
    병합, combine.children 없음) 이 pom 을 고쳐야 한다."""
    return any(PomEditor.child(PomEditor.child(el, "configuration"), "nonFilteredFileExtensions") is not None
               for el in ed.find_plugins(DEFAULT_PLUGIN_GROUP, artifact, "all"))


def _not_inherited(ed: PomEditor, artifact: str) -> bool:
    return any((PomEditor.text_of(el, "inherited") or "").lower() == "false"
               for el in ed.find_plugins(DEFAULT_PLUGIN_GROUP, artifact, "all"))


def _target(facts: dict[str, _Facts], path: str, artifact: str) -> str:
    """``path`` 의 빌드에 닿는 플러그인 설정을 가진 pom: 저장소 안 부모(보통 루트)가 필터링을 선언하고 그 설정이
    자식에게 내려가면 부모를 한 번 고친다; 역할 부모(트리 밖)의 모듈이면 자기 pom.

    The pom whose plugin configuration reaches `path`'s build: walk up in-tree parents while the parent itself
    declares filtering, the child has no plugin-level list of its own (it would replace the parent's) and the
    parent's declaration is inherited. A module on a role parent (outside the tree) is its own target."""
    cur, seen = path, {path}
    while True:
        f = facts[cur]
        par = f.tree_parent
        if par is None or par not in facts or par in seen:
            return cur
        if _has_own_list(f.ed, artifact):
            return cur
        pf = facts[par]
        if not pf.filters or _not_inherited(pf.ed, artifact):
            return cur
        seen.add(par)
        cur = par


def _resources_owner(facts: dict[str, _Facts], path: str) -> str | None:
    """``path`` 가 <resources> 를 물려받는 저장소 안 조상(자기 <resources> 가 없을 때만, 처음 선언한 조상이 필터링할
    때만)."""
    cur, seen = path, {path}
    while True:
        f = facts[cur]
        if f.own_resources:
            return cur if cur != path and f.filters_resources else None
        par = f.tree_parent
        if par is None or par not in facts or par in seen:
            return None
        seen.add(par)
        cur = par


def _scan(view: _View, pom: str) -> list[str]:
    try:
        return filtered_binary_resources(view, [SimpleNamespace(path=pom)])
    except Exception as e:  # noqa: BLE001 - the scan itself never raises; belt and braces
        log.debug("resources filtering: scan of %s failed: %s", pom, e)
        return []


def _add_files(w: _Want, files: Iterable[str]) -> None:
    for rel in files:
        w.files[rel] = _ext(rel)


# ---------------------------------------------------------------------------------------------- reactive
def _is_filtering_error(err: Any, text: str) -> bool:
    if not REACTIVE_RE.search(text):
        return False
    goal = (getattr(err, "goal", None) or "").lower()
    if goal and not goal.startswith(("resources:", "war:")) and not _FILE_RE.search(text):
        return False                              # another plugin's failure (compiler encoding, …)
    return bool(goal.startswith(("resources:", "war:")) or _PLUGIN_HINT_RE.search(text))


def _rel(ws: Any, path: str) -> str | None:
    p = str(path).strip().strip("'\"").replace("\\", "/")
    try:
        if p.startswith("/") or re.match(r"^[A-Za-z]:/", p):
            r = ws.rel(p).replace("\\", "/")
            if r.startswith("/") or re.match(r"^[A-Za-z]:/", r):
                return None                       # outside the workspace (another checkout's path)
            p = r
    except Exception:  # noqa: BLE001 - an odd path is just not named
        return None
    p = _norm(p)
    return p if p and not p.startswith("..") else None


def _nearest_pom(facts: dict[str, _Facts], rel: str) -> str | None:
    d = posixpath.dirname(rel)
    while True:
        cand = _norm(posixpath.join(d, "pom.xml")) if d else "pom.xml"
        if cand in facts:
            return cand
        if not d:
            return None
        d = posixpath.dirname(d)


def _reactive(ws: Any, facts: dict[str, _Facts], errors: Iterable[Any]) -> dict[str, _Want]:
    """빌드 오류 → 대상 pom 마다 (실패한 플러그인, 오류가 이름 댄 파일·확장자)."""
    out: dict[str, _Want] = {}
    if not facts:
        return out
    root = min(facts, key=lambda p: (p.count("/"), p))
    for err in errors or []:
        text = f"{getattr(err, 'message', '') or ''}\n{getattr(err, 'raw', '') or ''}"
        if not _is_filtering_error(err, text):
            continue
        goal = (getattr(err, "goal", None) or "").lower()
        artifact = WAR_PLUGIN if (goal.startswith("war:") or _WAR_HINT_RE.search(text)) else RESOURCES_PLUGIN
        named = [r for r in (_rel(ws, m.group("path")) for m in _FILE_RE.finditer(text)) if r]
        efile = getattr(err, "file", None)
        efile_rel = _rel(ws, efile) if efile else None
        if efile_rel and posixpath.basename(efile_rel) != "pom.xml" and efile_rel not in named:
            named.append(efile_rel)
        # 오류가 이름 댄 파일의 모듈이 가장 정확하다(재구성이 만든 모듈은 as-is 프로필이 모른다); 루트는 마지막
        by_file = [p for p in dict.fromkeys(_nearest_pom(facts, n) for n in named) if p]
        aids = {m.group("aid") for m in _PROJECT_RE.finditer(text)}
        if getattr(err, "module", None):
            aids.add(str(err.module))
        by_aid = [p for p, f in facts.items() if f.ed.artifact_id() in aids]
        poms = [p for p in by_file if p != root] or by_aid or by_file
        if not poms and efile_rel and efile_rel in facts:
            poms = [efile_rel]
        if not poms:
            poms = [p for p, f in facts.items() if f.filters]
        for p in poms:
            w = out.setdefault(_target(facts, p, artifact), _Want())
            w.plugins.add(artifact)
            _add_files(w, named)
    return out


def reactive_targets(ws: Any, errors: Iterable[Any], poms: Iterable[Any] | None = None) -> list[str]:
    """리소스 / war 플러그인의 필터링 복사 실패(``MalformedInputException`` …)에서 고칠 pom 들: 오류가 이름 댄 파일의
    모듈, ``on project <artifactId>`` 의 모듈, 없으면 필터링을 선언한 모든 pom(설정이 내려가는 부모로 올림). 해당
    오류가 없으면 빈 목록. 예외 없음.

    The poms to edit for a maven-resources-plugin / maven-war-plugin filtering failure (`errors` = the loop's
    `BuildError`s; message / raw matching `REACTIVE_RE`): the module of the file the message names, else the module
    `on project <artifactId>` / `BuildError.module` names, else every pom declaring filtering — each lifted to the
    in-tree parent whose plugin configuration reaches it. `binary_resource_plan(ws, cfg, poms, errors=errors)` is
    the plan for them. Empty when no error matches. Never raises."""
    try:
        facts = _load_facts(ws, _pom_paths(ws, poms))
        return sorted(_reactive(ws, facts, errors))
    except Exception as e:  # noqa: BLE001 - a failed lookup only means "no deterministic try"
        log.debug("resources filtering: reactive targets failed: %s: %s", type(e).__name__, e)
        return []


# ---------------------------------------------------------------------------------------------- plan
def binary_resource_plan(ws: Any, cfg: Any, poms: Iterable[Any] | None = None, *,
                         errors: Iterable[Any] | None = None) -> ResourcePlan:
    """무엇을 어느 pom 에 적을지: 필터링을 선언한 pom(또는 그 설정이 내려가는 저장소 안 부모)마다, 그 필터링 디렉토리
    아래 바이너리 파일의 확장자 + ``build.non_filtered_extensions`` 에서 플러그인이 이미 가진 것을 뺀 것. ``errors``
    를 주면(수정 루프) ``reactive_targets`` 의 pom 만, 오류가 이름 댄 파일의 확장자도 넣는다. 예외 없음.

    `poms` = pom paths (or objects with `.path`; None = every pom outside `src/`). For every pom whose plugin
    configuration reaches a filtered resource: the extensions of the binary files the analyzer's scan finds under
    it (known table or binary content, includes / excludes honoured) + the company additions, never the plugin's
    image defaults nor `TEXT_RESOURCE_EXTENSIONS`, minus the entries the element `apply_non_filtered_extensions`
    will extend already lists. A filtered binary without an extension → a follow-up. A module whose filtering comes
    only from a parent outside the tree is not in the plan (nothing in the tree declares it) — unless `errors`
    names it: then its default `src/main/resources` is scanned as filtered. With `errors` only the plugin that
    failed is extended (as v1 E12). Never raises (empty plan)."""
    try:
        return _plan(ws, cfg, poms, errors)
    except Exception as e:  # a failed plan means "nothing to write", never a failed restructure (logged)
        log.debug("resources filtering plan failed: %s: %s", type(e).__name__, e, exc_info=True)
        return ResourcePlan()


def _plan(ws: Any, cfg: Any, poms: Iterable[Any] | None, errors: Iterable[Any] | None) -> ResourcePlan:
    facts = _load_facts(ws, _pom_paths(ws, poms))
    if not facts:
        return ResourcePlan()
    view = _View(ws)
    company = _company_exts(cfg)
    want: dict[str, _Want] = {}
    for p, f in facts.items():
        owner = None if f.filters_resources else _resources_owner(facts, p)
        if not f.filters and owner is None:
            continue
        files = _scan(view, p) if f.filters else []
        if owner is not None:
            # 자식 모듈은 부모의 <resources> 를 자기 basedir 기준으로 물려받는다 → 부모 텍스트로 자식 디렉토리를 훑는다
            files += _scan(view.with_text(p, facts[owner].ed.to_string()), p)
        plugins = ([RESOURCES_PLUGIN] if (f.filters_resources or owner) else []) + \
                  ([WAR_PLUGIN] if f.filters_web else [])
        for artifact in plugins:
            w = want.setdefault(_target(facts, p, artifact), _Want())
            w.plugins.add(artifact)
            _add_files(w, files)
    if errors is not None:
        reactive = _reactive(ws, facts, errors)
        for t, rw in reactive.items():
            w = want.setdefault(t, _Want())
            if t not in facts or not (facts[t].filters or w.files):
                # 트리 밖 부모가 필터링을 켠 모듈: 기본 src/main/resources 를 필터링되는 것으로 보고 훑는다
                _add_files(w, _scan(view.with_text(t, _DEFAULT_FILTERED_POM), t))
            # 실패한 플러그인만 고친다(v1 E12 와 같음): war 실패에 resources 플러그인 목록까지 늘리지 않는다
            w.plugins = set(rw.plugins)
            w.files.update(rw.files)
        want = {t: w for t, w in want.items() if t in reactive}
    return _settle(ws, facts, want, company)


def _settle(ws: Any, facts: dict[str, _Facts], want: dict[str, _Want], company: list[str]) -> ResourcePlan:
    """원하는 것 − 이미 적힌 것 → ResourcePlan."""
    plan = ResourcePlan()
    noext: list[str] = []
    files: set[str] = set()
    for t in sorted(want):
        w = want[t]
        f = facts.get(t)
        if f is None:
            continue
        wanted = {e for e in w.files.values() if _usable(e)} | set(company)
        for rel, e in w.files.items():
            # nonFilteredFileExtensions 는 확장자로만 고른다 → 확장자 없는 바이너리는 사람이 나눠야 한다(후속 작업)
            if (not e or not _EXT_RE.fullmatch(e)) and rel not in noext:
                noext.append(rel)
        in_tree = f.tree_parent is not None or not f.has_parent
        added: set[str] = set()
        plugins: list[str] = []
        for artifact in PLUGIN_ORDER:
            if artifact not in w.plugins:
                continue
            have = set(_entries(_chosen_plugin(f.ed, artifact, in_tree)))
            missing = wanted - have
            if missing:
                plugins.append(artifact)
                added |= missing
        if added:
            plan.extensions_by_pom[t] = sorted(added)
            plan.plugins_by_pom[t] = plugins
        files |= {rel for rel, e in w.files.items() if e in added}
    plan.files = sorted(files | set(noext))
    noext.sort()
    for rel in noext[:MAX_FOLLOWUP_FILES]:
        plan.followups.append(f"binary file without an extension under a filtered resource dir: {rel} — exclude it "
                              "from filtering (a second <resource> with <filtering>false</filtering>) or rename it")
    if len(noext) > MAX_FOLLOWUP_FILES:
        plan.followups.append(f"… and {len(noext) - MAX_FOLLOWUP_FILES} more binary file(s) without an extension "
                              "under filtered resource dirs (see the structure review facts)")
    return plan


# ---------------------------------------------------------------------------------------------- apply
def _add_entries(ed: PomEditor, artifact: str, exts: list[str], in_tree_parent: bool) -> None:
    """플러그인 하나에 빠진 ``<nonFilteredFileExtension>`` 을 덧붙인다(기존 항목 유지, 플러그인 수준만). 선언이 없으면
    버전 없이 <build><plugins> 에 만든다(부모가 버전을 관리). 새로 만드는 목록에는 ``combine.children="append"`` 를
    단다 — 트리 밖 부모(comp-jar 등)가 이미 가진 목록을 Maven 설정 병합이 지우지 않게."""
    el = _chosen_plugin(ed, artifact, in_tree_parent)
    if el is None:
        ed.add_plugin(PluginInfo(group_id=DEFAULT_PLUGIN_GROUP, artifact_id=artifact,
                                 configuration={"nonFilteredFileExtensions": list(exts)}))
        made = ed.find_plugins(DEFAULT_PLUGIN_GROUP, artifact, "build")
        nfe = PomEditor.child(PomEditor.child(made[0], "configuration"), "nonFilteredFileExtensions") if made else None
        if nfe is not None:
            nfe.set(APPEND_ATTR, "append")
        return
    have = set(_entries(el))
    missing = [e for e in exts if e not in have]
    if not missing:
        return
    conf = PomEditor.child(el, "configuration")
    if conf is None:
        conf = ed.copy_into(el, etree.Element(ed.q("configuration")), ordered=True)
    nfe = PomEditor.child(conf, "nonFilteredFileExtensions")
    if nfe is None:
        src = etree.Element(ed.q("nonFilteredFileExtensions"))
        src.set(APPEND_ATTR, "append")
        for e in missing:
            etree.SubElement(src, ed.q("nonFilteredFileExtension")).text = e
        ed.copy_into(conf, src)
        return
    for e in missing:
        item = etree.Element(ed.q("nonFilteredFileExtension"))
        item.text = e
        ed.copy_into(nfe, item)


def apply_non_filtered_extensions(ws: Any, plan: ResourcePlan, *, poms: Iterable[str] | None = None) -> list[str]:
    """계획을 PomEditor 로 적고 실제로 바뀐 pom 목록을 돌려준다(``poms`` 를 주면 그 pom 만). 두 번째 호출은
    아무것도 바꾸지 않는다(멱등). 예외 없음.

    Write the plan through `PomEditor` (formatting kept, never text surgery): per pom and plugin the missing
    `<nonFilteredFileExtension>` entries are appended to the plugin-level `<configuration>` of the element
    `_chosen_plugin` picks (an execution's own `<configuration>` is left alone), created without a version when the
    pom declares none. Returns the changed poms (workspace-relative, as in the plan). Never raises."""
    only = {_norm(p) for p in poms} if poms is not None else None
    changed: list[str] = []
    for pom, exts in plan.extensions_by_pom.items():
        if not exts or (only is not None and _norm(pom) not in only):
            continue
        try:
            ed = PomEditor(ws.abs(pom))
            in_tree = _tree_parent(ws, _norm(pom), ed) is not None or ed.get_parent() is None
            for artifact in plan.plugins_by_pom.get(pom) or [RESOURCES_PLUGIN]:
                _add_entries(ed, artifact, list(exts), in_tree)
            if ed.changed:
                ed.save()
                changed.append(pom)
        except Exception as e:  # noqa: BLE001 - one unwritable pom never stops the others
            log.debug("nonFilteredFileExtensions not written to %s: %s: %s", pom, type(e).__name__, e)
    return changed


__all__ = [
    "PLUGIN_ORDER",
    "REACTIVE_RE",
    "RESOURCES_PLUGIN",
    "WAR_PLUGIN",
    "ResourcePlan",
    "apply_non_filtered_extensions",
    "binary_resource_plan",
    "reactive_targets",
]
