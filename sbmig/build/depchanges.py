"""v2.1d 의존성 버전 정책 — 시도(스크립트 / 편집)나 구조 검토 커밋이 pom 에서 실제로 무엇을
바꿨는지 읽어 내는 되읽기 가드. 왜: 회사 실행에서 LLM 이 SiteMesh 2(``opensymphony:sitemesh:2.4.2``)를
``org.sitemesh:sitemesh:3.2.1`` 로 바꾸고 필터를 옮겼다 — 빌드는 통과, 런타임은 미검증. 사용자 결정: 메이저는
올리지 않는다, 그래도 바뀌었으면 막지 말고(빌드는 통과해야 하니까) 운영자에게 무엇을 확인할지 알려 준다.
흐름상 위치: 수정 루프가 시도를 적용한 뒤와 구조 검토 커밋 뒤 ``changes_in_commit`` → major / replaced 는
``major_upgrades`` + 운영자 가이드(답변의 ``<verify>``, 없으면 LLM 1회, 그래도 없으면 ``synthesized_guide``) →
보고서 Handover 맨 위 "운영자 확인 필요 (메이저 업그레이드 N건)", ``SBMIG-RESULT … major=N``. 보고 때
``still_applied`` 로 되돌린 시도는 뺀다. 판정: 메이저 숫자가 바뀌면 ``major``, 라이브러리가 빠지고 대신하는
것이 들어오면 ``replaced``, 그 밖은 ``minor`` / ``added`` / ``removed``. 이 이유로는 아무것도 막거나 되돌리지
않는다. 텍스트만 받고 절대 예외 없음(파싱 안 되는 pom → []).

v2.1d dependency version policy — what a fix changed in the poms (docs/INTERFACES.md "v2.1d").

Why: in a company run the LLM replaced SiteMesh 2 (`opensymphony:sitemesh:2.4.2`) with `org.sitemesh:sitemesh:3.2.1`
and ported the filter; the build passed, the runtime was never verified. The user: never a major upgrade on the LLM's
own, and when one happened anyway, tell the operator what to check — without blocking the build. The prompt carries
the policy; this module is the read-back guard that notices what really changed.

`dependency_changes(before, after, path)` reads two versions of ONE pom and lists every coordinate change as
`DependencyChange` (dependencies, dependencyManagement, plugins, pluginManagement, `<parent>`): kind `major` when the
major number of the version changed, `replaced` when one library was removed and another added that stands in for it
(same artifactId, or one artifactId contains the other, or the old artifactId is the new groupId's last segment),
`minor` / `added` / `removed` otherwise. `${property}` versions are resolved from the same pom's `<properties>` where
possible. Pure text in, never raises (a pom that does not parse → []).

`changes_in_commit(ws, pre, sha)` does it for every `pom.xml` changed between two commits; `apply_table(changes,
libraries)` marks the changes a person's table entry allows; `still_applied(ws, change)` says whether the final tree
still carries it (a rolled-back try is not reported); `synthesized_guide(changes, lang)` is the operator guide sbmig
writes when the LLM gave no `<verify>`.


"""
from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from sbmig.core.models import DependencyChange

log = logging.getLogger("sbmig.build.depchanges")

SECTIONS = ("dependencies", "dependencyManagement", "plugins", "pluginManagement", "parent")
# 메이저 = 앞의 접두 글자(최대 3개, 'v' 등) 뒤 첫 숫자. ${…} 는 major_of 가 따로 None 처리.
_MAJOR_RE = re.compile(r"^\D{0,3}?(\d+)")
_PROP_RE = re.compile(r"\$\{([^}]+)\}")
_WORD_SPLIT_RE = re.compile(r"[-._]")
GUIDE_LINE_MAX = 160


def major_of(version: Optional[str]) -> Optional[int]:
    """Maven 버전의 앞 숫자(메이저): ``2.4.2`` → 2, ``3.3.0-RC1`` → 3, ``v2.1`` → 2; ``${x}`` / 빈 값 / None /
    ``RELEASE`` / ``LATEST`` → None(비교 불가 = major 로 보지 않음).

    Leading number of a Maven version (`2.4.2` → 2, `3.3.0-RC1` → 3, `v2.1` → 2; `${x}` / "" / None / `RELEASE`
    / `LATEST` → None)."""
    if version is None:
        return None
    v = str(version).strip()
    if not v or "${" in v:
        return None
    m = _MAJOR_RE.match(v)
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:  # pragma: no cover - the regex only matches digits
        return None


# ---------------------------------------------------------------------------------------------------------------------
# reading one pom
# ---------------------------------------------------------------------------------------------------------------------
@dataclass
class _Coord:
    group_id: str
    artifact_id: str
    version: Optional[str]           # property-resolved where possible; raw `${…}` when not; None = no <version>
    classifier: Optional[str] = None

    @property
    def key(self) -> str:
        return f"{self.group_id}:{self.artifact_id}"

    @property
    def ident(self) -> tuple[str, str, str]:
        return self.group_id, self.artifact_id, self.classifier or ""


def _resolver(ed: Any):
    """``${prop}`` 을 같은 pom 의 ``<properties>``(중첩은 몇 단계까지)와 ``${project.version}`` 류로 푼다. 못
    푸는 것은 쓴 그대로 둔다. 버전을 프로퍼티(``<sitemesh.version>``)로만 바꾼 시도도 잡아내려고 푼다.

    `${prop}` → the pom's own `<properties>` (nested references followed a few levels), `${project.version}` /
    `${pom.version}` → the pom's version (or its parent's); what cannot be resolved stays as written."""
    props = ed.properties()
    own = ed.effective_version() or ""
    builtins = {"project.version": own, "pom.version": own, "version": own,
                "project.groupId": ed.effective_group_id() or "", "project.artifactId": ed.artifact_id() or ""}
    parent = ed.get_parent()
    if parent is not None and parent.version:
        builtins["project.parent.version"] = parent.version

    def resolve(value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        v = value.strip()
        for _ in range(5):
            if "${" not in v:
                break
            nv = _PROP_RE.sub(lambda m: props.get(m.group(1), builtins.get(m.group(1)) or m.group(0)), v)
            if nv == v:
                break
            v = nv
        return v or None

    return resolve


def _coords(ed: Any) -> dict[str, list[_Coord]]:
    """pom 하나의 좌표를 구역별로(dependencies / dependencyManagement / plugins / pluginManagement /
    parent), 버전은 프로퍼티를 푼 값."""
    resolve = _resolver(ed)
    out: dict[str, list[_Coord]] = {s: [] for s in SECTIONS}
    for section, managed in (("dependencies", False), ("dependencyManagement", True)):
        for el in ed.dependency_elements(managed):
            gav = ed.gav_of(el)
            if not gav.artifact_id:
                continue
            out[section].append(_Coord(gav.group_id, gav.artifact_id, resolve(gav.version), gav.classifier or None))
    for section, where in (("plugins", "build"), ("pluginManagement", "management")):
        for el in ed.plugin_elements(where):
            g, a = ed.plugin_key(el).split(":", 1)
            if not a:
                continue
            out[section].append(_Coord(g, a, resolve(ed.text_of(el, "version") or None)))
    parent = ed.get_parent()
    if parent is not None and parent.artifact_id:
        out["parent"].append(_Coord(parent.group_id, parent.artifact_id, resolve(parent.version)))
    return out


def _parse(text: str, path: str) -> Any:
    """텍스트 → PomEditor. 비었거나 pom 이 아니면 None(새 pom / 지운 pom 은 비교하지 않는다).

    PomEditor of `text`, None when it is empty / not a pom."""
    if not text or not text.strip():
        return None
    from sbmig.restructure.pom_editor import PomEditor
    try:
        return PomEditor(path or "pom.xml", text=text)
    except Exception as e:  # noqa: BLE001 - PomEditError, encoding trouble: no changes from this pom
        log.debug("depchanges: %s does not parse: %s", path, e)
        return None


# ---------------------------------------------------------------------------------------------------------------------
# diffing two texts
# ---------------------------------------------------------------------------------------------------------------------
def _version_kind(before: Optional[str], after: Optional[str]) -> str:
    mb, ma = major_of(before), major_of(after)
    return "major" if mb is not None and ma is not None and mb != ma else "minor"


def _words(artifact_id: str) -> list[str]:
    return [w for w in _WORD_SPLIT_RE.split(artifact_id.lower()) if w]


def _contains_word(longer: str, shorter: str) -> bool:
    """짧은 쪽이 긴 쪽의 접두어이거나 단어 단위로 들어 있는가(``sitemesh`` ⊂
    ``spring-boot-starter-sitemesh``). 4글자 미만은 정확히 같을 때만(짧은 이름이 아무 데나 맞지 않게).

    `shorter` is a prefix of `longer` or a whole word run inside it (`sitemesh` in `spring-boot-starter-sitemesh`,
    `spring-boot-starter-web` → `spring-boot-starter-webmvc`). Very short ids (< 4 chars) only match exactly."""
    lo, sh = longer.lower(), shorter.lower()
    if len(sh) < 4 or lo == sh:
        return lo == sh
    if lo.startswith(sh):
        return True
    lw, sw = _words(lo), _words(sh)
    n = len(sw)
    return any(lw[i:i + n] == sw for i in range(len(lw) - n + 1)) if n else False


def _pair_rank(old: _Coord, new: _Coord) -> Optional[int]:
    """빠진 라이브러리 ``old`` 를 새 ``new`` 가 대신하는 정도(작을수록 좋음): 0 = 같은 artifactId, 1 = 한쪽이
    다른 쪽을 단어로 포함, 2 = 옛 artifactId 가 새 groupId 의 마지막 칸, None = 관계없음.

    How well `new` stands in for `old` (lower = better), None = unrelated."""
    oa, na = old.artifact_id.lower(), new.artifact_id.lower()
    if oa == na:
        return 0
    a, b = (oa, na) if len(oa) >= len(na) else (na, oa)
    if _contains_word(a, b):
        return 1
    if oa == new.group_id.lower().rsplit(".", 1)[-1]:
        return 2
    return None


def _note_for(c: _Coord) -> str:
    return f"{c.key}:{c.classifier}" if c.classifier else ""


def _section_changes(section: str, before: list[_Coord], after: list[_Coord], path: str) -> list[DependencyChange]:
    """한 구역의 전후 좌표 비교. parent 는 하나뿐이라 g:a 가 바뀌면 replaced. 같은 (g, a, classifier) 의
    버전 변경 → major / minor. 같은 g:a 에서 classifier 만 바뀐 것(ehcache → ehcache:jakarta)은 교체가 아니라
    버전 변경(note 에 classifier). 남은 빠짐 + 들어옴은 ``_pair_rank`` 가 좋은 짝부터 replaced 로
    묶고, 나머지는 removed / added."""
    out: list[DependencyChange] = []
    b_by: dict[tuple, _Coord] = {}
    for c in before:
        b_by.setdefault(c.ident, c)
    a_by: dict[tuple, _Coord] = {}
    for c in after:
        a_by.setdefault(c.ident, c)

    if section == "parent":                               # at most one parent: a changed g:a is a replacement
        ob, oa = next(iter(b_by.values()), None), next(iter(a_by.values()), None)
        if ob and oa:
            if ob.key != oa.key:
                out.append(DependencyChange(path=path, key=ob.key, before=ob.version, after=oa.version,
                                            kind="replaced", replaced_by=oa.key, section="parent"))
            elif ob.version != oa.version:
                out.append(DependencyChange(path=path, key=ob.key, before=ob.version, after=oa.version,
                                            kind=_version_kind(ob.version, oa.version), section="parent"))
        elif ob:
            out.append(DependencyChange(path=path, key=ob.key, before=ob.version, kind="removed", section="parent"))
        elif oa:
            out.append(DependencyChange(path=path, key=oa.key, after=oa.version, kind="added", section="parent"))
        return out

    removed: list[_Coord] = []
    for ident, ob in b_by.items():
        oa = a_by.get(ident)
        if oa is None:
            removed.append(ob)
        elif ob.version != oa.version:
            out.append(DependencyChange(path=path, key=ob.key, before=ob.version, after=oa.version,
                                        kind=_version_kind(ob.version, oa.version), section=section,
                                        note=_note_for(oa)))
    added = [oa for ident, oa in a_by.items() if ident not in b_by]

    # the same g:a with another classifier (ehcache → ehcache:jakarta) is a version / classifier change
    for ob in list(removed):
        twin = next((oa for oa in added if oa.key == ob.key), None)
        if twin is None:
            continue
        removed.remove(ob)
        added.remove(twin)
        kind = _version_kind(ob.version, twin.version)
        out.append(DependencyChange(path=path, key=ob.key, before=ob.version, after=twin.version, kind=kind,
                                    section=section,
                                    note=f"classifier {ob.classifier or 'none'} → {twin.classifier or 'none'}"))

    # removed + added that stand in for each other → replaced (best match first; each pairing consumes both)
    pairs: list[tuple[int, int, int]] = []
    for i, ob in enumerate(removed):
        for j, oa in enumerate(added):
            r = _pair_rank(ob, oa)
            if r is not None:
                pairs.append((r, i, j))
    used_r: set[int] = set()
    used_a: set[int] = set()
    replaced: list[tuple[int, DependencyChange]] = []
    for r, i, j in sorted(pairs):
        if i in used_r or j in used_a:
            continue
        used_r.add(i)
        used_a.add(j)
        ob, oa = removed[i], added[j]
        note = " ".join(n for n in (_note_for(ob), _note_for(oa)) if n)
        replaced.append((i, DependencyChange(path=path, key=ob.key, before=ob.version, after=oa.version,
                                             kind="replaced", replaced_by=oa.key, section=section, note=note)))
    out += [c for _i, c in sorted(replaced, key=lambda t: t[0])]
    out += [DependencyChange(path=path, key=ob.key, before=ob.version, kind="removed", section=section,
                             note=_note_for(ob)) for i, ob in enumerate(removed) if i not in used_r]
    out += [DependencyChange(path=path, key=oa.key, after=oa.version, kind="added", section=section,
                             note=_note_for(oa)) for j, oa in enumerate(added) if j not in used_a]
    return out


def dependency_changes(before: str, after: str, path: str) -> list[DependencyChange]:
    """pom 하나(``path``, 프로젝트 기준 posix)의 두 텍스트 사이 좌표 변경 목록. 절대 예외 없음;
    한쪽이 비었거나 파싱 안 되면 [].

    Coordinate changes between two texts of the pom at `path` (project-relative posix). Never raises; a side
    that is empty or does not parse → []."""
    try:
        path = (path or "pom.xml").replace("\\", "/")
        eb, ea = _parse(before, path), _parse(after, path)
        if eb is None or ea is None:
            return []
        cb, ca = _coords(eb), _coords(ea)
        out: list[DependencyChange] = []
        for section in SECTIONS:
            out += _section_changes(section, cb[section], ca[section], path)
        return out
    except Exception as e:
        log.debug("dependency_changes(%s) failed: %s", path, e, exc_info=True)
        return []


def changes_in_commit(ws: Any, pre: str, sha: str) -> list[DependencyChange]:
    """두 커밋(``pre`` → ``sha``) 사이에 바뀐 모든 ``pom.xml`` 에 대해 ``dependency_changes``
    (``ws.file_at(sha, path)`` = ``git show``). git 이 답하지 못하면 [], 절대 예외 없음.

    `dependency_changes` over every pom.xml changed between commits `pre` and `sha` (`ws.file_at(sha, path)`).
    Never raises; [] when git cannot tell."""
    try:
        if not pre or not sha or pre == sha:
            return []
        if hasattr(ws, "changed_between"):
            files = list(ws.changed_between(pre, sha))
        else:
            cp = ws._git("diff", "--name-only", pre, sha, check=False)
            files = [f for f in (cp.stdout or "").splitlines() if f.strip()]
    except Exception as e:  # noqa: BLE001 - git trouble: nothing to report
        log.debug("changes_in_commit(%s..%s): %s", pre, sha, e)
        return []
    out: list[DependencyChange] = []
    for f in files:
        rel = str(f).replace("\\", "/").strip()
        if rel.rsplit("/", 1)[-1] != "pom.xml":
            continue
        try:
            before = _file_at(ws, pre, rel) or ""
            after = _file_at(ws, sha, rel) or ""
        except Exception as e:  # noqa: BLE001
            log.debug("changes_in_commit: %s at %s/%s: %s", rel, pre, sha, e)
            continue
        out += dependency_changes(before, after, rel)
    return out


def _file_at(ws: Any, sha: str, rel: str) -> Optional[str]:
    if hasattr(ws, "file_at"):
        return ws.file_at(sha, rel)
    cp = ws._git("show", f"{sha}:{rel}", check=False)                  # pragma: no cover - older workspaces
    return cp.stdout if cp.returncode == 0 else None


# ---------------------------------------------------------------------------------------------------------------------
# the people's table
# ---------------------------------------------------------------------------------------------------------------------
def _entry_for(libraries: list[Any], key: Optional[str]) -> Optional[Any]:
    if not key:
        return None
    return next((e for e in libraries if key in (getattr(e, "all_keys", None) or [getattr(e, "key", None)])), None)


def _first_sentence(text: str) -> str:
    from sbmig.core.jakarta_libraries import first_sentence
    return first_sentence(text or "")


def apply_table(changes: Iterable[DependencyChange], libraries: Iterable[Any]) -> None:
    """사람이 넣는 라이브러리 표(``JakartaLibrary``: 기본 표 + 회사 ``libraries.yml``)가 허락한 변경에
    ``approved`` / ``note`` 를 단다. 승인은 (1) 그 항목의 ``allow_major``(변경이 항목 버전의 메이저 줄에
    떨어질 때) 또는 (2) 같은 메이저 안에서 항목 ``version`` == ``after`` 인 고정뿐이다. 메이저를 건너는 표
    버전은 '사실' 이지 승인이 아니다(규칙 14) → note 만 달리고 운영자 확인 항목으로 남는다. 절대 예외 없음.

    Set `approved` / `note` on the changes a table entry (`JakartaLibrary`) allows:
    * `allow_major` on the entry of `key` or `replaced_by` — a person allowed the move to the entry's `version`
      (the change must land on that major line, or carry no version);
    * the entry's `version` == `after` for the library the entry describes (`key`'s own entry, or for a `replaced`
      change an entry whose coordinates cover both `key` and `replaced_by`, i.e. a relocation the table knows).
    Otherwise a `major` / `replaced` change whose OLD library has a same-major table release gets the note
    `the table offers a same-major jakarta release: <version>`. Never raises."""
    try:
        libs = list(libraries or [])
        for ch in changes:
            try:
                _apply_one(ch, libs)
            except Exception as e:  # noqa: BLE001
                log.debug("apply_table(%s): %s", getattr(ch, "key", "?"), e)
    except Exception as e:  # noqa: BLE001
        log.debug("apply_table: %s", e)


def _apply_one(ch: DependencyChange, libs: list[Any]) -> None:
    """변경 하나에 표 적용: 새 라이브러리(``replaced_by``)의 항목을 먼저, 다음 옛
    라이브러리(``key``)의 항목."""
    old_e = _entry_for(libs, ch.key)
    new_e = _entry_for(libs, ch.replaced_by) if ch.replaced_by else None
    target_key = ch.replaced_by or ch.key
    for e in [x for x in (new_e, old_e) if x is not None]:
        version = getattr(e, "version", None)
        keys = set(getattr(e, "all_keys", None) or [getattr(e, "key", "")])
        on_line = ch.after is None or ch.after == version or (
            major_of(ch.after) is not None and major_of(ch.after) == major_of(version))
        covers = target_key in keys and (ch.kind != "replaced" or ch.key in keys)
        # 표의 고정 버전은 같은 메이저 안에서만 승인이다. 메이저를 건너는 이동은 사람의 allow_major 만
        # 승인한다 — 기본 표의 버전은 사실일 뿐(SiteMesh 2 → 3 교체를 기본 표가 승인하지 않는다).
        # a pin (`version` == after) approves only inside the major line: a cross-major move is approved by a person's
        # `allow_major` alone — the shipped table's cross-major versions are facts, not approvals (rule 14)
        same_line = major_of(ch.before) is None or major_of(ch.after) is None or \
            major_of(ch.before) == major_of(ch.after)
        pinned = covers and version is not None and version == ch.after
        if (getattr(e, "allow_major", False) and on_line) or (pinned and same_line):
            ch.approved = True
            src = getattr(e, "source", "shipped")
            first = _first_sentence(getattr(e, "note", ""))
            ch.note = f"approved by the {src} table" + (f": {first}" if first else "")
            return
        if pinned and ch.needs_operator:
            ch.note = f"the {getattr(e, 'source', 'shipped')} table knows this jakarta release (no same-major jakarta build listed)"
            return
    if ch.needs_operator and old_e is not None:
        v = getattr(old_e, "version", None)
        if v and major_of(v) is not None and major_of(v) == major_of(ch.before):
            ch.note = f"the table offers a same-major jakarta release: {v}"


# ---------------------------------------------------------------------------------------------------------------------
# the final tree
# ---------------------------------------------------------------------------------------------------------------------
def _read(ws: Any, rel: str) -> Optional[str]:
    if hasattr(ws, "read"):
        try:
            return ws.read(rel)
        except Exception as e:  # noqa: BLE001 - missing / unreadable: try the plain file below
            log.debug("still_applied: ws.read(%s): %s", rel, e)
    if hasattr(ws, "abs"):
        try:
            return Path(ws.abs(rel)).read_text(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            return None
    return None


def still_applied(ws: Any, change: DependencyChange) -> bool:
    """최종 작업 트리의 ``change.path`` pom 이 아직 그 변경(``key`` / ``replaced_by`` 의 ``after`` 버전)을
    담고 있는가? 보고 때 되돌려진 시도를 운영자 확인 목록에서 빼려고 쓴다. 절대 예외 없음(못 읽으면 False).

    Does the working tree's pom at `change.path` still carry the change (the `after` version of `key` /
    `replaced_by`)? Used at report time to drop rolled-back tries. Never raises (unreadable → False)."""
    try:
        text = _read(ws, change.path)
        ed = _parse(text or "", change.path)
        if ed is None:
            return False
        coords = _coords(ed).get(change.section, [])

        def present(key: Optional[str], version: Optional[str], *, any_version: bool) -> bool:
            hits = [c for c in coords if c.key == key]
            if not hits:
                return False
            return any_version or any(c.version == version for c in hits)

        if change.kind == "removed":
            return not any(c.key == change.key for c in coords)
        if change.kind == "added":
            return present(change.key, change.after, any_version=True)
        if change.kind == "replaced":
            return present(change.replaced_by, change.after, any_version=change.after is None)
        return present(change.key, change.after, any_version=change.after is None)
    except Exception as e:  # noqa: BLE001
        log.debug("still_applied(%s): %s", getattr(change, "key", "?"), e)
        return False


# ---------------------------------------------------------------------------------------------------------------------
# the operator guide sbmig writes itself
# ---------------------------------------------------------------------------------------------------------------------
def _cap(line: str) -> str:
    return line if len(line) <= GUIDE_LINE_MAX else line[: GUIDE_LINE_MAX - 1].rstrip() + "…"


def _majors(ch: DependencyChange) -> str:
    mb, ma = major_of(ch.before), major_of(ch.after)
    return f"{mb}.x → {ma}.x" if mb is not None and ma is not None and mb != ma else ""


def synthesized_guide(changes: Iterable[DependencyChange], lang: str = "ko") -> str:
    """LLM 이 ``<verify>`` 를 주지 않았을 때 sbmig 가 직접 쓰는 운영자 확인 사항: 메이저 업그레이드 /
    라이브러리 교체마다 한 덩어리(좌표와 건넌 메이저, 설정 파일·filter / servlet / listener 등록·바뀐
    패키지·런타임 확인, 표 승인이면 그 메모). ``lang`` 이 ko 로 시작하면 한국어, 아니면
    영어(``report.language``); 줄마다 160자 이하; 운영자 확인이 필요 없으면 "".

    The 운영자 확인 사항 sbmig writes when the LLM gave none: one block per major upgrade / replaced library (the
    rest is ignored) — the coordinates and the major line crossed, then the generic checks (configuration files /
    registrations / renamed packages / runtime) and, when a person's table approved it, that note. Korean when
    `lang` starts with "ko", else English; every line ≤ 160 chars; "" when no change needs the operator."""
    try:
        ko = (lang or "ko").lower().startswith("ko")
        blocks: list[str] = []
        seen: set[tuple] = set()
        for ch in changes or []:
            if not getattr(ch, "needs_operator", False):
                continue
            ident = (ch.path, ch.key, ch.replaced_by, ch.before, ch.after)
            if ident in seen:
                continue
            seen.add(ident)
            majors = _majors(ch)
            lib = ch.replaced_by or ch.key
            if ko:
                if ch.kind == "replaced":
                    head = (f"{ch.text} ({ch.path}): 라이브러리가 다른 라이브러리로 바뀌었습니다"
                            + (f" ({majors})" if majors else "") + " — 패키지·설정·등록 방식이 달라질 수 있습니다")
                else:
                    head = (f"{ch.text} ({ch.path}): 메이저 버전이 바뀌었습니다"
                            + (f" ({majors})" if majors else "") + " — API·설정이 달라질 수 있습니다")
                lines = [head,
                         f"- {lib} 의 설정 파일(XML / properties / yaml)이 새 버전 형식에 맞는지 확인하세요",
                         "- web.xml / Java 설정의 filter · servlet · listener 등록과 클래스 이름이 새 버전과 맞는지 확인하세요",
                         "- 프로젝트 코드의 바뀐 패키지·삭제된 클래스를 확인하세요 (MIGRATION-TODO 표시를 검색)",
                         f"- {lib} 를 쓰는 화면·배치를 실제로 실행해 확인하세요 (런타임 스모크 테스트)"]
                if ch.approved:
                    lines.append(f"- 사람이 라이브러리 표에서 승인한 변경입니다: {ch.note}" if ch.note
                                 else "- 사람이 라이브러리 표에서 승인한 변경입니다")
            else:
                if ch.kind == "replaced":
                    head = (f"{ch.text} ({ch.path}): replaced by another library"
                            + (f" ({majors})" if majors else "") + " — packages, configuration and registration may differ")
                else:
                    head = (f"{ch.text} ({ch.path}): the major version changed"
                            + (f" ({majors})" if majors else "") + " — API and configuration may differ")
                lines = [head,
                         f"- check that {lib}'s configuration files (XML / properties / yaml) match the new version",
                         "- check the filter / servlet / listener registrations (web.xml / Java config) and their class names",
                         "- check renamed packages and removed classes in the project's code (search for MIGRATION-TODO)",
                         f"- run the screens / jobs that use {lib} (runtime smoke test)"]
                if ch.approved:
                    lines.append(f"- a person approved this change in the library table: {ch.note}" if ch.note
                                 else "- a person approved this change in the library table")
            blocks.append("\n".join(_cap(x) for x in lines))
        return "\n\n".join(blocks)
    except Exception as e:  # noqa: BLE001
        log.debug("synthesized_guide: %s", e)
        return ""
