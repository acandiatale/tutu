"""Maven 리소스 필터링 대상 폴더에 있는 바이너리 파일을 필터링에서 제외하는 모듈.
maven-resources-plugin이 웹폰트, 압축 파일, ``.dmdb`` 같은 파일을 텍스트로 읽다가
``MalformedInputException: Input length = 1``로 실패하는 것을 LLM 없이 막는다.
재구성 단계가 첫 빌드 전에 제외 확장자를 기록하고, 빌드 오류가 나면 수정 루프의 사전
시도(``reactive_targets``)가 다시 기록한다.
- 필터링 자체는 끄지 않는다. ``@project.version@`` 같은 값은 계속 치환되어야 하기 때문이다
- 파일은 지우지 않고, 플러그인 기본값과 텍스트 확장자는 기록하지 않는다
- 예외를 던지지 않고, 여러 번 실행해도 결과가 같다

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
# 리소스나 war 플러그인의 복사 실패만 다루고 javac 인코딩 오류 같은 다른 실패는 건드리지 않는다
_PLUGIN_HINT_RE = re.compile(r"maven-resources-plugin|maven-war-plugin|\b(?:resources|war):[\w-]+\b|"
                             r"filtering \S.* to \S")
_WAR_HINT_RE = re.compile(r"maven-war-plugin|\bwar:(?:war|exploded|inplace)\b")
_EXT_RE = re.compile(r"[a-z0-9][a-z0-9_+\-]{0,15}")
# 자식 pom이 자기 <resources>를 선언하지 않을 때만 부모의 <build><resources>가 상속된다
_DEFAULT_FILTERED_POM = ("<project><build><resources><resource><directory>src/main/resources</directory>"
                         "<filtering>true</filtering></resource></resources></build></project>")
APPEND_ATTR = "combine.children"
MAX_FOLLOWUP_FILES = 10


class ResourcePlan(BaseModel):
    """pom별로 추가할 제외 확장자와 대상 플러그인을 담는 데이터 클래스.
    아직 처리되지 않은 바이너리 파일(``files``)과 확장자가 없어 직접 처리해야 할 파일(``followups``)도 함께 담는다.

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
        """추가할 확장자가 하나도 없는지 확인한다. 후속 작업(``followups``)은 따로 확인해야 한다."""
        return not any(self.extensions_by_pom.values())

    def lines(self, only: Iterable[str] | None = None) -> list[str]:
        """보고서에 쓸 ``<pom>: ext1, ext2 -> maven-resources-plugin`` 형식의 줄 목록을 만든다.
        ``only``를 주면 그 안에 있는 pom만 포함한다.

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
    """제외 확장자 계획에 필요한 pom 하나의 정보를 담는 클래스. pom 파싱은 한 번만 한다."""
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
    """대상 pom 하나에 기록할 플러그인, 파일, 확장자를 모으는 클래스. 아직 중복을 제거하기 전의 값이다."""
    plugins: set[str] = field(default_factory=set)
    exts: set[str] = field(default_factory=set)
    files: dict[str, str] = field(default_factory=dict)       # file → its extension ("" = none usable)


class _View:
    """``filtered_binary_resources``에 넘길 작업 공간 뷰 클래스.
    파일 목록은 한 번만 읽고, 부모의 <resources> 설정으로 자식 모듈을 검사할 때는 pom 텍스트를 바꿔서 보여 준다."""

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
    """파일 이름에서 Maven과 같은 방식으로 확장자를 구해 반환한다.
    마지막 점 뒤를 소문자로 돌려주고, 점이 없으면 빈 문자열을 반환한다."""
    base = posixpath.basename(str(name).replace("\\", "/"))
    return base.rsplit(".", 1)[1].lower() if "." in base else ""


def _usable(ext: str) -> bool:
    """pom에 기록해도 되는 확장자인지 확인한다.
    형식이 맞고 플러그인 기본값이나 텍스트 확장자가 아니어야 한다."""
    return bool(_EXT_RE.fullmatch(ext)) and ext not in PLUGIN_UNFILTERED_DEFAULTS \
        and ext not in TEXT_RESOURCE_EXTENSIONS


def _company_exts(cfg: Any) -> list[str]:
    """설정 ``build.non_filtered_extensions``에 지정된 회사 확장자(예: dmdb)를 반환한다.
    소문자로 바꾸고 점을 빼며, 기록할 수 없는 값은 버린다."""
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
    """이 pom의 <parent>가 저장소 안에 있으면 그 pom 경로를 반환한다.
    relativePath가 비어 있으면 저장소 밖 부모로 보고, artifactId가 일치해야 같은 부모로 인정한다."""
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
    """pom이 resources / testResources를 필터링하는지와 webResources를 필터링하는지를 튜플로 반환한다.
    주석 처리된 설정은 세지 않는다."""
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
    """수정할 플러그인 요소를 찾는다.
    <build><plugins> 선언을 먼저 쓰고, 저장소 밖 역할 부모를 상속하는 모듈이 아니면 <pluginManagement>
    선언도 쓴다. 둘 다 없으면 None을 반환하므로 호출하는 쪽이 새로 만든다.

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
    """플러그인 수준 ``<configuration><nonFilteredFileExtensions>``에 있는 확장자 목록을 소문자, 점 없는
    형태로 반환한다. execution 안의 configuration은 보지 않는다."""
    if el is None:
        return []
    nfe = PomEditor.child(PomEditor.child(el, "configuration"), "nonFilteredFileExtensions")
    return [(x.text or "").strip().lower().lstrip(".") for x in PomEditor.children(nfe, "nonFilteredFileExtension")]


def _has_own_list(ed: PomEditor, artifact: str) -> bool:
    """이 pom이 플러그인 수준의 제외 확장자 목록을 직접 갖고 있는지 확인한다.
    갖고 있으면 combine.children이 없는 한 부모 목록을 덮어쓰므로 이 pom을 고쳐야 한다."""
    return any(PomEditor.child(PomEditor.child(el, "configuration"), "nonFilteredFileExtensions") is not None
               for el in ed.find_plugins(DEFAULT_PLUGIN_GROUP, artifact, "all"))


def _not_inherited(ed: PomEditor, artifact: str) -> bool:
    return any((PomEditor.text_of(el, "inherited") or "").lower() == "false"
               for el in ed.find_plugins(DEFAULT_PLUGIN_GROUP, artifact, "all"))


def _target(facts: dict[str, _Facts], path: str, artifact: str) -> str:
    """``path`` 모듈의 빌드에 적용되는 플러그인 설정을 가진 pom을 찾아 반환한다.
    저장소 안 부모의 필터링 설정을 상속하면 부모를, 역할 부모를 상속하는 모듈이면 자기 pom을 반환한다.

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
    """``path`` 모듈이 <resources> 설정을 상속받는 저장소 안 조상 pom을 반환한다.
    자기 <resources>가 없고, 그것을 처음 선언한 조상이 필터링을 켰을 때만 반환하며, 아니면 None을 반환한다."""
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
    """빌드 오류에서 대상 pom별로 실패한 플러그인, 오류에 나온 파일, 확장자를 뽑아 반환한다."""
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
        # 오류에 나온 파일이 속한 모듈을 먼저 쓴다.
        # 재구성이 만든 모듈은 원래 프로젝트 정보로는 찾을 수 없기 때문이며, 루트 pom은 마지막에 쓴다
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
    """리소스나 war 플러그인의 필터링 복사 실패에서 고쳐야 할 pom 목록을 찾는다.
    오류에 나온 파일의 모듈, ``on project <artifactId>``의 모듈, 필터링을 선언한 모든 pom 순으로 찾는다.
    해당 오류가 없으면 빈 목록을 반환하고, 예외를 던지지 않는다.

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
    """pom별로 기록할 제외 확장자를 계획한다.
    필터링 폴더 아래 바이너리 파일의 확장자와 ``build.non_filtered_extensions``의 확장자 중 이미 기록된 것을 뺀다.
    ``errors``를 주면 ``reactive_targets``가 찾은 pom만 다루고 오류에 나온 파일의 확장자도 넣는다.
    예외를 던지지 않는다.

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
            # 자식 모듈은 부모의 <resources>를 자기 basedir 기준으로 상속받으므로 부모 텍스트로 자식 폴더를 검사한다
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
                # 저장소 밖 부모가 필터링을 켠 모듈은 기본 경로 src/main/resources가 필터링된다고 보고 검사한다
                _add_files(w, _scan(view.with_text(t, _DEFAULT_FILTERED_POM), t))
            # war 플러그인이 실패했을 때 resources 플러그인까지 고치지 않도록 실패한 플러그인만 대상으로 한다
            w.plugins = set(rw.plugins)
            w.files.update(rw.files)
        want = {t: w for t, w in want.items() if t in reactive}
    return _settle(ws, facts, want, company)


def _settle(ws: Any, facts: dict[str, _Facts], want: dict[str, _Want], company: list[str]) -> ResourcePlan:
    """기록할 내용에서 이미 pom에 기록된 것을 빼고 ResourcePlan을 만든다."""
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
            # nonFilteredFileExtensions는 확장자로만 파일을 고르므로 확장자 없는 바이너리 파일은 직접 처리해야 한다
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
    """플러그인 하나에 빠진 ``<nonFilteredFileExtension>`` 항목을 추가한다.
    플러그인 선언이 없으면 버전 없이 <build><plugins>에 새로 만든다.
    새 목록에는 ``combine.children="append"``를 달아 저장소 밖 부모의 목록이 설정 병합 때 지워지지 않게 한다."""
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
    """계획을 pom에 반영하고 실제로 바뀐 pom 목록을 반환한다. ``poms``를 주면 그 pom만 고친다.
    두 번 호출해도 결과가 같고, 예외를 던지지 않는다.

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
