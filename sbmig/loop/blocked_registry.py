"""더 이상 고치지 않기로 한 오류(차단)를 모아 두는 모듈. 차단한 오류는 다시 시도하지 않고 보고서에는 남긴다.

docs/ERROR-HANDLING.md: "never loop on what we cannot fix, never hide it".

The orchestrator makes one registry per run and hands it to every ``BuildFixLoop``. A blocked error is listed
in the report's "Blocked" and Handover sections with what to do. Reasons include repeated failure,
``loop.blocked_policy``, no LLM, an exhausted budget, a missing company parent.

Keys are (L1 signature, file): the full project-relative posix path for compile errors, '' for module /
artifact errors. The full path keeps MSA services apart (services keep their directories). Restructure moves
(`src/…` -> `{name}-back/src/…`, `module/comp-jar/src/…`, merged modules) are followed through `note_moves`,
which the orchestrator calls with the step's git renames; `canonical_file` maps an old path to its new one.

`BlockedItem.suggestion` comes from the reason, the attributed artifact (`sbmig.build.classpath`, optional,
lazily imported) and `cfg.artifacts.optional`. `fill_via` adds `BlockedItem.via` / `via_direct` from the
`sbmig.build.deptree` inventory and recomposes it (docs/ERROR-HANDLING.md §5).
"""
from __future__ import annotations

import hashlib
import importlib
import logging
import re
from typing import Any, Iterable, Optional

from sbmig.core.models import (GAV, BlockedItem, BlockReason, BuildError, ErrorCluster, ErrorKind, Phase)

log = logging.getLogger("sbmig.loop")

FILE_KINDS = (ErrorKind.COMPILE, ErrorKind.TEST_COMPILE)
MAX_ERRORS = 10
MAX_RAW = 2000
MAX_MOVE_HOPS = 8                    # 여러 재구성 단계로 옮겨진 파일을 따라가는 최대 횟수


def normalize_file(path: Optional[str]) -> Optional[str]:
    """파일 경로에서 ``src/`` 이후 부분만 잘라서 반환한다.

    Not the registry key (that is the full path + `note_moves`); kept for callers that want the suffix."""
    if not path:
        return path
    p = path.replace("\\", "/")
    m = re.search(r"(?:^|/)(src/(?:main|test)/.*)$", p)
    return m.group(1) if m else p


def posix_path(path: Optional[str]) -> str:
    """경로 구분자를 '/'로 통일하고 앞의 './'를 제거한다. 값이 없으면 빈 문자열을 반환한다."""
    p = (path or "").replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p


def error_key(e: BuildError) -> str:
    """차단 목록에서 오류를 구분하는 키 문자열을 만든다. 컴파일 오류는 서명 뒤에 ``@<파일>``을 붙인다.

    No move mapping; `BlockedRegistry.key` is the move-aware tuple the registry itself uses."""
    sig = e.signature or f"{e.kind.value}|{e.message}"
    if e.kind in FILE_KINDS and e.file:
        return f"{sig}@{posix_path(e.file)}"
    return sig


def sanitize_errors(errors: Iterable[BuildError], limit: int = MAX_ERRORS) -> list[BuildError]:
    """보고서와 저널에 기록할 오류 목록을 만든다.
    최대 ``limit``개까지 담고 Maven 로그 원문은 ``MAX_RAW``자까지만 남긴다."""
    out: list[BuildError] = []
    for e in list(errors)[:limit]:
        raw = e.raw if len(e.raw or "") <= MAX_RAW else e.raw[:MAX_RAW] + " …"
        out.append(e.model_copy(update={"raw": raw}))
    return out


Key = tuple[str, str]                # (L1 서명, 파일 경로 또는 '')


class BlockedRegistry:
    """실행 한 번 동안 차단된 오류를 모아 두는 클래스.
    같은 사유로 차단된 같은 오류는 한 항목으로 합친다. 그래서 단계가 바뀌어도 항목 id(``blk-<해시>``)가 그대로다."""
    def __init__(self) -> None:
        self._items: dict[str, BlockedItem] = {}
        self._by_key: dict[Key, str] = {}
        self._moves: dict[str, str] = {}             # 재구성이 옮긴 파일의 이전 경로와 새 경로

    # -- 키와 파일 이동 --------------------------------------------------------
    def canonical_file(self, path: Optional[str]) -> str:
        """재구성 단계에서 위치가 바뀐 파일의 현재 경로를 반환한다.
        여러 번 옮겨졌으면 최대 ``MAX_MOVE_HOPS``번까지 따라간다."""
        p = posix_path(path)
        for _ in range(MAX_MOVE_HOPS):
            nxt = self._moves.get(p)
            if not nxt or nxt == p:
                break
            p = nxt
        return p

    def key(self, e: BuildError) -> Key:
        """차단 목록에서 오류를 찾을 키(L1 서명, 현재 파일 경로)를 만든다.
        모듈이나 아티팩트 오류는 파일 경로가 빈 문자열이다."""
        sig = e.signature or f"{e.kind.value}|{e.message}"
        if e.kind in FILE_KINDS and e.file:
            return sig, self.canonical_file(e.file)
        return sig, ""

    def note_moves(self, moves: dict[str, str]) -> int:
        """재구성 단계가 옮긴 파일에 맞춰 차단 키를 새 경로로 바꾸고, 바꾼 키 수를 반환한다.
        옮기기 전에 차단된 파일은 옮긴 뒤에도 차단 상태로 남는다.
        MSA나 boot_pom 실행은 디렉토리를 옮기지 않아서 바꿀 것이 없다."""
        clean = {posix_path(a): posix_path(b) for a, b in (moves or {}).items() if a and b and a != b}
        if not clean:
            return 0
        self._moves.update(clean)
        moved = 0
        for (sig, f), iid in list(self._by_key.items()):
            if not f:
                continue
            nf = self.canonical_file(f)
            if nf != f:
                del self._by_key[(sig, f)]
                self._by_key.setdefault((sig, nf), iid)
                moved += 1
        return moved

    # -- 조회 -------------------------------------------------------------
    def __len__(self) -> int:
        return len(self._items)

    def __iter__(self):
        return iter(self._items.values())

    def items(self) -> list[BlockedItem]:
        """차단 항목 전체를 반환한다."""
        return list(self._items.values())

    def get(self, item_id: str) -> Optional[BlockedItem]:
        """id로 차단 항목을 찾는다. 없으면 None을 반환한다."""
        return self._items.get(item_id)

    def item_for_error(self, e: BuildError) -> Optional[BlockedItem]:
        """이 오류를 차단하고 있는 항목을 찾는다. 없으면 None을 반환한다."""
        iid = self._by_key.get(self.key(e))
        return self._items.get(iid) if iid else None

    def is_blocked_error(self, e: BuildError) -> bool:
        """이 오류가 이미 차단됐는지 반환한다.
        마무리 단계에서 미해결 목록과 차단 목록이 겹치지 않게 할 때도 쓴다."""
        return self.key(e) in self._by_key

    def partition(self, cluster: ErrorCluster) -> tuple[list[BuildError], list[BuildError]]:
        """클러스터의 오류를 (이미 차단된 오류, 아직 처리할 오류)로 나눈다.
        일부만 차단된 케이스는 나머지 오류만 계속 고친다."""
        blocked, rest = [], []
        for e in cluster.errors:
            (blocked if self.is_blocked_error(e) else rest).append(e)
        return blocked, rest

    def covering(self, cluster: ErrorCluster) -> Optional[BlockedItem]:
        """클러스터의 오류가 모두 차단돼 있으면 첫 차단 항목을 반환한다.
        하나라도 차단되지 않았으면 None을 반환한다."""
        if not cluster.errors:
            return None
        items = [self.item_for_error(e) for e in cluster.errors]
        if any(i is None for i in items):
            return None
        return items[0]

    # -- 변경 ------------------------------------------------------------
    def block(self, cluster: ErrorCluster, reason: BlockReason, phase: Phase, *,
              errors: Optional[list[BuildError]] = None, iteration: int = 0, attempts: int = 0,
              required_type: Optional[str] = None, found_type: Optional[str] = None,
              owner_class: Optional[str] = None, artifact: Optional[GAV] = None, suggestion: str = "",
              notes: Optional[list[str]] = None) -> tuple[BlockedItem, bool]:
        """클러스터의 오류 전체나 일부를 차단하고 (항목, 새로 만들었는지)를 반환한다.
        같은 사유의 기존 항목이 이 오류 중 하나를 이미 갖고 있으면 그 항목에 합친다.
        그래서 라이브러리 문제 하나는 보고서에 한 줄로 나오고 단계가 바뀌어도 id가 유지된다.

        id = `blk-` + sha1(reason + sorted keys)[:10]."""

        errs = list(errors if errors is not None else cluster.errors) or list(cluster.errors)
        keys = [self.key(e) for e in errs]
        existing = next((self._items[self._by_key[k]] for k in keys if k in self._by_key
                         and self._items[self._by_key[k]].reason == reason), None)
        if existing is not None:
            self._merge(existing, errs, keys)
            existing.attempts = max(existing.attempts, attempts)
            return existing, False
        flat = sorted({f"{s}@{f}" if f else s for s, f in keys})
        digest = hashlib.sha1(("|".join([reason.value, *flat])).encode("utf-8")).hexdigest()[:10]
        files: list[str] = []
        for e in errs:
            f = e.file or (e.artifact.coordinates if e.artifact else None)
            if e.file and e.line:
                f = f"{e.file}:{e.line}"
            if f and f not in files:
                files.append(f)
        item = BlockedItem(id=f"blk-{digest}", reason=reason, phase=phase, cluster_id=cluster.id,
                           signatures=list(dict.fromkeys(e.signature or e.message for e in errs)), files=files,
                           errors=sanitize_errors(errs), artifact=artifact, owner_class=owner_class,
                           required_type=required_type, found_type=found_type, suggestion=suggestion,
                           attempts=attempts, first_seen_iteration=iteration, notes=list(notes or []))
        self._items[item.id] = item
        for k in keys:
            self._by_key.setdefault(k, item.id)
        return item, True

    def _merge(self, item: BlockedItem, errs: list[BuildError], keys: list[Key]) -> None:
        for e, k in zip(errs, keys):
            if k not in self._by_key:
                self._by_key[k] = item.id
            sig = e.signature or e.message
            if sig not in item.signatures:
                item.signatures.append(sig)
            f = f"{e.file}:{e.line}" if e.file and e.line else (e.file or None)
            if f and f not in item.files:
                item.files.append(f)
            if len(item.errors) < MAX_ERRORS and all(x.raw != e.raw or x.file != e.file for x in item.errors):
                item.errors += sanitize_errors([e], 1)

    def remove(self, item_id: str) -> Optional[BlockedItem]:
        """차단 항목 하나와 그 항목을 가리키는 키를 모두 지운다. 지운 항목을 반환하고, 없으면 None이다."""
        item = self._items.pop(item_id, None)
        if item is not None:
            for k in [k for k, v in self._by_key.items() if v == item_id]:
                del self._by_key[k]
        return item


# ---------------------------------------------------------------------------------------------------------------------
# 원인 라이브러리 찾기(소유 클래스 -> jar -> GAV). sbmig.build.classpath는 없어도 된다
# ---------------------------------------------------------------------------------------------------------------------
def default_locator() -> Any:
    """클래스 이름으로 jar, GAV, 모듈을 찾는 기본 locator(``sbmig.build.classpath``)를 반환한다.
    모듈이 없거나 깨졌으면 None을 반환한다. 그때는 원인 라이브러리를 찾지 않는다."""
    try:
        return importlib.import_module("sbmig.build.classpath")
    except Exception as e:  # 모듈이 없거나 깨졌으면 원인 라이브러리를 비워 둔다
        log.debug("sbmig.build.classpath unavailable: %s", e)
        return None


def attribute(locator: Any, ws, cfg, owner_class: Optional[str], errors: list[BuildError],
              module: Optional[str] = None) -> tuple[Optional[GAV], Optional[str], list[str]]:
    """오류의 원인 라이브러리를 classpath에서 찾아 (아티팩트, 소유 클래스, 메모)를 반환한다.
    소유 클래스가 프로젝트 안에 있으면 외부 라이브러리 문제가 아니다. 아티팩트는 비워 둔다.
    찾다가 실패해도 예외를 던지지 않고 메모 한 줄을 남긴다.

    Uses `locator.locate_class`, else `locator.locate_type_owner` on the first three errors."""
    notes: list[str] = []
    if locator is None:
        return None, owner_class, notes
    loc = None
    try:
        if owner_class and hasattr(locator, "locate_class"):
            loc = locator.locate_class(ws, cfg, owner_class, module)
        if loc is None and hasattr(locator, "locate_type_owner"):
            for e in errors[:3]:
                loc = locator.locate_type_owner(ws, cfg, e, module)
                if loc is not None:
                    break
    except Exception as e:
        notes.append(f"classpath attribution failed: {type(e).__name__}: {e}")
        return None, owner_class, notes
    if loc is None:
        return None, owner_class, notes
    if getattr(loc, "in_project", False):
        notes.append(f"owner class located in the project ({getattr(loc, 'jar', '')})")
        return None, owner_class, notes
    owner = getattr(loc, "owner_class", None) or getattr(loc, "fqcn", None) or owner_class
    jar = getattr(loc, "jar", None)
    if jar:
        notes.append(f"owner jar: {jar}")
    return getattr(loc, "artifact", None), owner, notes


# ---------------------------------------------------------------------------------------------------------------------
# javax API jar를 끌어오는 라이브러리(via) 찾기. sbmig.build.deptree 목록을 쓴다
# ---------------------------------------------------------------------------------------------------------------------
# javax 패키지와 그 패키지를 담은 API 아티팩트. jar를 못 찾아도 오류가 요구하는 타입으로 어느 API인지 알 수 있다
# 같은 API 계열의 다른 group은 _API_FAMILY_GROUPS에 두고 같은 jar로 본다
JAVAX_PACKAGE_ARTIFACTS: dict[str, str] = {
    "javax.persistence": "javax.persistence:javax.persistence-api",
    "javax.servlet": "javax.servlet:javax.servlet-api",
    "javax.validation": "javax.validation:validation-api",
    "javax.annotation": "javax.annotation:javax.annotation-api",
    "javax.transaction": "javax.transaction:javax.transaction-api",
    "javax.inject": "javax.inject:javax.inject",
    "javax.xml.bind": "javax.xml.bind:jaxb-api",
    "javax.mail": "javax.mail:javax.mail-api",
    "javax.jms": "javax.jms:javax.jms-api",
    "javax.websocket": "javax.websocket:javax.websocket-api",
    "javax.el": "javax.el:javax.el-api",
}
_API_FAMILY_GROUPS: dict[str, tuple[str, ...]] = {
    "javax.persistence": ("javax.persistence", "org.hibernate.javax.persistence", "jakarta.persistence"),
    "javax.servlet": ("javax.servlet", "jakarta.servlet"),
    "javax.validation": ("javax.validation", "jakarta.validation"),
    "javax.annotation": ("javax.annotation", "jakarta.annotation"),
    "javax.transaction": ("javax.transaction", "jakarta.transaction"),
    "javax.inject": ("javax.inject", "jakarta.inject"),
    "javax.xml.bind": ("javax.xml.bind", "jakarta.xml.bind"),
    "javax.mail": ("javax.mail", "com.sun.mail", "jakarta.mail"),
    "javax.jms": ("javax.jms", "jakarta.jms"),
    "javax.websocket": ("javax.websocket", "jakarta.websocket"),
    "javax.el": ("javax.el", "jakarta.el"),
}
_VIA_REASONS = (BlockReason.EXTERNAL_JAVAX_API, BlockReason.EXTERNAL_API_INCOMPATIBLE)


def is_javax_api_artifact(gav: Optional[GAV]) -> bool:
    """javax EE API jar인지 반환한다.
    group이 ``javax.*``, ``org.hibernate.javax.persistence``, ``com.sun.mail``이면 참이다.
    이름만 jakarta인 재배포본도 참이다. ``jakarta.*:*-api`` 1.x와 2.x, servlet 4.x, el 3.x는 패키지가 아직 javax다."""
    if gav is None:
        return False
    g = gav.group_id
    if g.startswith("javax.") or g == "javax" or g.startswith("org.hibernate.javax.persistence") \
            or g.startswith("com.sun.mail"):
        return True
    if g.startswith("jakarta.") and gav.artifact_id.endswith("-api") and gav.version:
        major = gav.version.split(".", 1)[0]
        if major in ("1", "2") or (major == "4" and "servlet" in gav.artifact_id) \
                or (major == "3" and gav.artifact_id == "jakarta.el-api"):
            return True
    return False


def _javax_package(item: BlockedItem) -> Optional[str]:
    for t in (item.required_type, item.owner_class, item.found_type):
        if not t or not t.startswith("javax."):
            continue
        best = max((p for p in JAVAX_PACKAGE_ARTIFACTS if t == p or t.startswith(p + ".")), key=len, default=None)
        if best:
            return best
    return None


def _item_module(item: BlockedItem) -> Optional[str]:
    return next((e.module for e in item.errors if e.module), None)


def _jar_module_names(jar: Any) -> set[str]:
    names = {getattr(jar, "module", None) or ""}
    mod = getattr(jar, "module", None)
    if mod:
        names.add(mod.rstrip("/").rsplit("/", 1)[-1])
    path = list(getattr(jar, "path", None) or [])
    if path:
        parts = path[0].split(":")
        if len(parts) > 1:
            names.add(parts[1])
    return {n for n in names if n}


def _jar_rank(jar: Any) -> int:
    via = getattr(jar, "via", None)
    if via is not None:
        try:
            from sbmig.core.jakarta_libraries import jakarta_library_for
            if jakarta_library_for(via.key) is not None:
                return 0
        except Exception:  # pragma: no cover - 지식 테이블을 읽지 못한 경우
            pass
        return 1
    return 2 if getattr(jar, "declared", False) else 3


def match_javax_jar(item: BlockedItem, inventory: Iterable[Any]) -> Any:
    """javax API나 맞지 않는 외부 API 때문에 차단된 항목에 맞는 javax jar를 목록에서 찾는다. 없으면 None이다.
    항목의 아티팩트를 먼저 본다. 없으면 오류에 나온 javax 패키지의 API 아티팩트로 찾는다. 같은 모듈의 jar를 우선한다.

    Returns a `JavaxApiJar`; ties ranked by `_jar_rank` (known jakarta library via, any via, declared,
    rest)."""
    jars = list(inventory or [])
    if not jars:
        return None
    a = item.artifact
    keys: set[str] = set()
    groups: tuple[str, ...] = ()
    if a is not None:
        keys.add(a.key)
    pkg = _javax_package(item)
    if pkg:
        keys.add(JAVAX_PACKAGE_ARTIFACTS[pkg])
        groups = _API_FAMILY_GROUPS.get(pkg, ())
    if not keys:
        return None
    cands = [j for j in jars if j.artifact.key in keys]
    if not cands and groups:
        cands = [j for j in jars if j.artifact.group_id in groups]
    if not cands:
        return None
    mod = _item_module(item)
    if mod:
        m = mod.strip("/")
        local = [j for j in cands if m in _jar_module_names(j) or m.rsplit("/", 1)[-1] in _jar_module_names(j)]
        cands = local or cands
    return sorted(cands, key=_jar_rank)[0]


def fill_via(items: Iterable[BlockedItem], inventory: Iterable[Any], cfg=None) -> int:
    """javax 차단 항목에 그 javax jar를 끌어오는 직접 의존성(``via``)을 채운다. 최종 빌드 뒤 ``dependency:tree``로 찾는다.
    조치 문구도 다시 만들고, 바뀐 항목 수를 반환한다.
    javax API jar를 추가하라고 하지 않고, 그 라이브러리를 jakarta 버전으로 바꾸라고 안내하려는 것이다.

    Only items of `_VIA_REASONS` whose artifact is None or a javax API jar."""
    inv = list(inventory or [])
    changed = 0
    for item in items:
        if item.reason not in _VIA_REASONS:
            continue
        if item.artifact is not None and not is_javax_api_artifact(item.artifact):
            continue
        jar = match_javax_jar(item, inv)
        if jar is not None:
            item.via = list(jar.path)
            item.via_direct = jar.via
            if item.artifact is None:
                item.artifact = jar.artifact.model_copy(update={"scope": None})
            if item.via_direct is not None:
                note = "via: " + " → ".join(item.via)
                if note not in item.notes:
                    item.notes.append(note)
        new = compose_suggestion(item, cfg, javax_jar=jar)
        if jar is not None or new != item.suggestion:
            changed += 1
        item.suggestion = new
    return changed


def _in_house_groups(cfg) -> list[str]:
    arts = getattr(cfg, "artifacts", None) if cfg is not None else None
    if arts is None:
        return []
    out: list[str] = []
    try:
        for s in [*arts.parent_specs(), *arts.mandatory, *arts.optional]:
            if s.group_id and s.group_id not in out:
                out.append(s.group_id)
    except Exception:  # pragma: no cover
        pass
    return out


def is_in_house(gav: Optional[GAV], cfg) -> bool:
    """사내 아티팩트인지 반환한다.
    설정된 parent, 필수, 선택, 역할 parent 아티팩트의 group이거나 그 하위 group이면 참이다."""
    if gav is None:
        return False
    g = gav.group_id
    return any(g == x or g.startswith(x + ".") for x in _in_house_groups(cfg))


def _module_label(item: BlockedItem, jar: Any) -> str:
    mod = getattr(jar, "module", None) if jar is not None else None
    if mod:
        return f"module `{mod}`"
    path = list(getattr(jar, "path", None) or item.via or [])
    if path:
        parts = path[0].split(":")
        if len(parts) > 1:
            return f"`{parts[1]}`"
    m = _item_module(item)
    return f"module `{m}`" if m else "the root module"


def _api_jar_text(item: BlockedItem, jar: Any) -> str:
    gav = getattr(jar, "artifact", None) if jar is not None else None
    gav = gav or item.artifact
    return gav.coordinates if gav is not None else "the javax API jar"


def _via_suggestion(item: BlockedItem, cfg, jar: Any = None) -> str:
    """javax jar를 끌어오는 경로를 찾은 항목의 조치 문구를 만든다. 경로가 없으면 빈 문자열을 반환한다.
    1. 표에 있는 라이브러리는 jakarta 버전으로 올리라고 한다. 좌표가 바뀌었으면 좌표도 바꾸라고 한다
    2. 사내 아티팩트는 사내 jakarta 빌드가 필요하다고 한다
    3. 그 밖의 라이브러리는 업그레이드나 교체가 필요하다고 한다
    4. pom에 직접 선언된 javax jar는 삭제하라고 한다"""
    via = item.via_direct
    declared = bool(getattr(jar, "declared", False)) if jar is not None else (
        via is None and len(item.via) == 2)
    if via is None and not declared:
        return ""
    api = _api_jar_text(item, jar)
    where = _module_label(item, jar)
    if via is not None:
        lib = None
        try:
            from sbmig.core.jakarta_libraries import jakarta_library_for
            lib = jakarta_library_for(via.key)
        except Exception as e:  # pragma: no cover - 지식 테이블을 읽지 못한 경우
            log.debug("jakarta_libraries unavailable: %s", e)
        if lib is not None and via.key != lib.key:                                           # (1) 좌표 변경
            java = f", requires Java {lib.requires_java}" if lib.requires_java else ""
            return (f"`{via.coordinates}` is `{lib.key}` under its earlier coordinates → `{lib.key}:{lib.version}` "
                    f"(jakarta.* since {lib.jakarta_since}{java}): relocate the declaration (it drags in `{api}`, "
                    f"direct dependency of {where})")
        if lib is not None:                                                                   # (1)
            java = f" (requires Java {lib.requires_java})" if lib.requires_java else ""
            note = (lib.note or "").split(";", 1)[0].strip()
            return (f"`{api}` comes through `{via.coordinates}` (direct dependency of {where}): `{lib.key}` moved to "
                    f"jakarta.* in {lib.jakarta_since} — raise it to {lib.version}{java}"
                    + (f" ({note})" if note else ""))
        if is_in_house(via, cfg):                                                             # (2)
            line = getattr(getattr(getattr(cfg, "artifacts", None), "parent", None), "version", None)
            on = f"on the {line} line" if line else "on the configured in-house version line"
            return (f"`{via.coordinates}` is an in-house artifact still compiled against javax (it drags in "
                    f"`{api}`): it needs a Jakarta build {on}; add it to `artifacts.optional` with that version so "
                    f"the optional bump moves it")
        return (f"`{api}` comes through `{via.coordinates}` (direct dependency of {where}), which is still compiled "  # (3)
                f"against javax: upgrade it to a Jakarta release (check its changelog) or replace it; left unchanged "
                f"on purpose")
    pom = f"`{jar.module}/pom.xml`" if jar is not None and getattr(jar, "module", None) else (
        "the root `pom.xml`" if jar is not None else f"the pom of {where}")
    return (f"`{api}` is declared in {pom} — remove it (its jakarta.* successor comes with the Spring Boot "           # (4)
            f"starters)")


# ---------------------------------------------------------------------------------------------------------------------
# 조치 문구
# ---------------------------------------------------------------------------------------------------------------------
def _optional_spec(cfg, artifact: Optional[GAV]):
    if cfg is None or artifact is None:
        return None
    try:
        return cfg.optional_keys().get(artifact.key)
    except Exception:  # pragma: no cover
        return None


_REACTOR_COORDS_RE = re.compile(r"Project '(?P<c>[^'\s]+)' is duplicated in the reactor")


def reactor_duplicate_suggestion(item: BlockedItem) -> str:
    """``reactor_duplicate`` 차단 항목의 조치 문구를 만든다. 같은 모듈이 ``<module>``에 두 번 나오면 하나로 정리하라는 안내다.
    안쪽 aggregator pom의 중복은 접수 단계가 이미 지운다. 여기 오는 것은 접수가 지우지 못한 경우다.
    ``sbmig/report/i18n.py``의 ``PATTERNS``가 이 문장을 한국어로 바꾼다. 문장을 고치면 그쪽도 함께 고친다."""
    coords = item.artifact.coordinates if item.artifact is not None else None
    if coords is None:
        m = next((m for m in (_REACTOR_COORDS_RE.search(f"{e.message}\n{e.raw or ''}") for e in item.errors) if m),
                 None)
        coords = m.group("c") if m else "the project"
    return (f"`{coords}` is reached more than once through the reactor's <module> lists (e.g. the root pom lists DEV "
            f"and a nested aggregator lists ../DEV): keep one listing per module — the intake removes the duplicate "
            f"listings of nested aggregators, this one it could not (two listings in the root pom, two directories "
            f"with the same coordinates, or a profile); fix the poms by hand, the LLM is not asked")


# sbmig/report/i18n.py의 PATTERNS가 이 문장을 한국어로 바꾼다. 문장을 고치면 그쪽도 함께 고친다
FILE_LOCK_SUGGESTION = ("the build output (target/) was locked by another process: exclude the project's target/ "
                        "from the antivirus real-time scan, close any IDE / Explorer window / leftover java process "
                        "holding it, then run again; the LLM is not asked")


def compose_suggestion(item: BlockedItem, cfg=None, *, settings_file: Optional[str] = None,
                       repositories: Optional[list[str]] = None, extra: str = "", javax_jar: Any = None) -> str:
    """차단 항목의 조치 문구를 만든다. 보고서의 "Blocked"와 인수인계 섹션에 사람이 할 다음 일로 나간다.
    문구는 사유마다 다르다. 예를 들어 없는 아티팩트는 Nexus 프록시나 좌표 수정을 안내한다. LLM에게 버전을 짓게 하지 않는다.

    The javax reasons try `_via_suggestion` first (after `fill_via`), then the optional-artifact /
    attributed-jar texts."""
    r = item.reason
    a = item.artifact
    if r in (BlockReason.EXTERNAL_JAVAX_API, BlockReason.EXTERNAL_API_INCOMPATIBLE):
        what = (f"still takes `{item.required_type}`" if r == BlockReason.EXTERNAL_JAVAX_API and item.required_type
                else "exposes an API this code no longer matches")
        via_text = _via_suggestion(item, cfg, javax_jar)
        if via_text:
            return via_text
        spec = _optional_spec(cfg, a)
        if a is not None and spec is not None:
            v = spec.version or "the configured version"
            return (f"`{spec.key}` is a configured optional artifact; its version bump to `{v}` happens in the "
                    f"restructure — if this is still blocked after the bump, `{v}` itself still exposes javax: ask "
                    f"the library owner for a Jakarta build")
        if a is not None:
            owner = f" (class `{item.owner_class}`)" if item.owner_class else ""
            if is_javax_api_artifact(a):
                # 찾은 jar가 javax API jar 자체다. 그 jar를 올리라고 하지 않고, 그 jar를 끌어오는 라이브러리를 찾는 방법을 안내한다
                return (f"`{a.coordinates}`{owner} {what}; the javac error only names the javax type; run "
                        f"`mvn dependency:tree -Dincludes={a.key}` in the module to find the library that still "
                        f"uses it, then upgrade that library to a Jakarta release or replace it")
            if r == BlockReason.EXTERNAL_JAVAX_API:
                return (f"`{a.coordinates}`{owner} {what}; upgrade `{a.key}` to a Jakarta build (add it to "
                        f"artifacts.optional with that version) or wrap the call with a shim; left unchanged on purpose")
            return (f"`{a.coordinates}`{owner} {what}; upgrade `{a.key}` to a compatible version or adapt the call by "
                    f"hand; left unchanged on purpose")
        if item.owner_class:
            return (f"`{item.owner_class}` {what}; owner class `{item.owner_class}` could not be attributed to an "
                    f"artifact; check the module classpath (mvn dependency:tree) and upgrade that library"
                    + (" to a Jakarta build" if r == BlockReason.EXTERNAL_JAVAX_API else ""))
        return (f"a library API {what}" + (f" (project supplies `{item.found_type}`)" if item.found_type else "")
                + "; the owning class could not be determined — check the module classpath (mvn dependency:tree) "
                  "for a javax-based library and upgrade it to a Jakarta build")
    if r == BlockReason.ARTIFACT_NOT_FOUND:
        coords = a.coordinates if a else "the artifact"
        repos = ", ".join(repositories or []) or "the configured repositories"
        return (f"`{coords}` is not in any repository: proxy it in Nexus or fix the coordinates "
                f"(checked repositories: {repos}); the LLM is never asked to invent versions")
    if r == BlockReason.REPOSITORY_UNREACHABLE:
        coords = a.coordinates if a else "the artifact"
        repos = f" ({', '.join(repositories)})" if repositories else ""
        return (f"`{coords}` could not be downloaded: the repository did not answer{repos} (timeout, refused connection or "
                "server error) — not a project problem: check the repository and the network, then rerun")
    if r == BlockReason.PARENT_NOT_FOUND:
        coords = a.coordinates if a else "the parent POM"
        s = settings_file or "default ~/.m2/settings.xml"
        repos = f" (checked: {', '.join(repositories)})" if repositories else ""
        return (f"parent `{coords}` is not resolvable{repos} with settings `{s}`: check build.settings_file / the "
                f"repository mirror and artifacts.parent; nothing else can build until it resolves")
    if r == BlockReason.PLUGIN_INCOMPATIBLE:
        p = extra or "the plugin"
        return (f"build plugin `{p}` fails on JDK 21: upgrade it (raise its version in the pom) or remove/replace "
                f"it; left unchanged on purpose")
    if r == BlockReason.UNSUPPORTED_SOURCE:
        return ("non-Java / generated sources (Kotlin, Groovy, Scala, protobuf/jaxb/openapi generators) are not "
                "migrated by sbmig: migrate this module or generator configuration by hand")
    if r == BlockReason.ENCODING:
        return "the source encoding could not be determined: set project.build.sourceEncoding or convert to UTF-8"
    if r == BlockReason.REACTOR_DUPLICATE:
        return reactor_duplicate_suggestion(item)
    if r == BlockReason.FILE_LOCK:
        return FILE_LOCK_SUGGESTION
    if r == BlockReason.BUDGET_EXHAUSTED:
        return ("the run's LLM/time budget was used up before this cluster could be fixed: rerun with a higher "
                "loop.max_llm_calls / loop.max_total_minutes or fix it by hand")
    if r == BlockReason.LLM_UNAVAILABLE:
        return ("the LLM was unavailable for this run (proven stored fix scripts were still reused): fix the LLM "
                "endpoint / key and rerun, or fix it by hand" + (f" — last error: {extra}" if extra else ""))
    if r == BlockReason.REPEATED_FAILURE:
        return (f"{item.attempts} fix attempt(s) did not resolve it; needs a human (see the fix attempts and the "
                "journal for what was tried)")
    if r == BlockReason.OSCILLATION:
        return "fixes kept toggling between the same error sets; needs a human (see the fix attempts)"
    if r == BlockReason.NEEDS_OWNER:
        # 보통은 수정 루프가 LLM이 준 차단 사유 문장을 넣는다. 그 문장이 없을 때만 이 문구를 쓴다

        return ("the LLM named a blocker outside the project (a library without a jakarta build, a framework to "
                "replace, an artifact the repository lacks) — 소유팀 결정 필요")
    return "needs a human"
