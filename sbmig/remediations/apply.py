"""취약점 조치의 적용·검증: 후보 사본의 pom 에 승인된 **버전 글자만** 바꿔 넣고, 바뀐 것이 정말 그것뿐인지와 Maven 해석 결과가
계획과 같은지를 확인한다. 흐름상 위치: plan(planner.py) → **apply** (``apply_proposals`` → ``verify_text_only``) → build →
**verify** (``verify_resolution``). planner 도 제안의 diff 를 만들 때 ``render_diff`` 를 부른다.

지키는 결정:
- 변경은 줄 단위 글자 치환뿐이다: ``edit.pom`` 의 ``edit.line`` 번째 줄에 ``edit.old`` 가 정확히 한 번 있어야 하고 그 자리만
  ``edit.new`` 로 바뀐다. 그 밖의 바이트(인코딩·BOM·줄 끝 CRLF·마지막 줄바꿈)는 그대로다 — XML 을 다시 쓰지 않는다.
- 모든 제안의 edit 을 **먼저** 검사하고, 잘못된 edit 이 하나라도 있는 제안은 통째로 건너뛴다(반쯤 적용하지 않는다).
- 경로는 프로젝트 폴더 안이어야 한다(``..``·절대 경로·심볼릭 링크 탈출 거부). ``old`` / ``new`` 는 버전 글자처럼 보여야 한다.

Apply + verify for the remediation feature: approved version-text replacements on a candidate copy, the "only version
text changed" check and the "resolution matches the plan" check. All edits are validated first; a proposal with one
invalid edit is skipped whole. Bytes outside the replaced span (encoding, BOM, CRLF, trailing newline) are preserved.
"""
from __future__ import annotations

import codecs
import difflib
import logging
import os
import re
from pathlib import Path, PurePosixPath

from .models import (K_BOM_VERSION, K_MANAGED_OVERRIDE, K_PARENT_VERSION, K_PROPERTY_VERSION, ApplyResult, PackagedLib,
                     Proposal, ResolvedModel, TextEdit)

log = logging.getLogger("sbmig.remediations.apply")

# 버전 글자로 받아들이는 모양: 출력 가능한 ASCII 이고 XML 특수문자·공백·따옴표가 없다. ``${`` 는 속성 참조라 버전 글자가 아니다.
_VERSION_TEXT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._\-+]*")
_XML_DECL = re.compile(rb"^<\?xml[^>]*?encoding\s*=\s*[\"']([A-Za-z0-9._\-]+)[\"']")
_BOMS = ((codecs.BOM_UTF8, "utf-8"), (codecs.BOM_UTF32_LE, "utf-32-le"), (codecs.BOM_UTF32_BE, "utf-32-be"),
         (codecs.BOM_UTF16_LE, "utf-16-le"), (codecs.BOM_UTF16_BE, "utf-16-be"))
# 속성(property) 종류의 제안은 같은 속성을 쓰는 다른 아티팩트도 함께 움직인다 — 그 움직임은 문제가 아니라 정보다.
_SHARED_KINDS = (K_PROPERTY_VERSION, K_MANAGED_OVERRIDE, K_PARENT_VERSION, K_BOM_VERSION)


class EditError(ValueError):
    """edit 하나가 규칙을 어겼다(경로·줄·글자·인코딩). 메시지가 ``ApplyResult.skipped`` 의 이유가 된다.

    An edit broke a rule; the message becomes the skip reason."""


def is_version_text(text: str) -> bool:
    """버전 글자처럼 보이는가: 비어 있지 않고 ``<``·``>``·``&``·공백·따옴표·``$`` 가 없다.

    True when `text` looks like plain version text (no markup, whitespace, quotes or property reference)."""
    return bool(text) and _VERSION_TEXT.fullmatch(text) is not None


# ---------------------------------------------------------------------------------------------------- 파일 읽기
class _PomText:
    """pom 하나의 바이트 ↔ 글자. 인코딩은 BOM → XML 선언 → UTF-8 순으로 정하고, 다시 인코딩했을 때 원래 바이트가 그대로
    나오는 경우만 고친다(그래야 바꾼 자리 밖의 바이트가 보존된다)."""

    def __init__(self, data: bytes, name: str):
        self.raw = data
        self.bom = b""
        self.encoding = "utf-8"
        for bom, enc in _BOMS:
            if data.startswith(bom):
                self.bom, self.encoding = bom, enc
                break
        body = data[len(self.bom):]
        if not self.bom:
            m = _XML_DECL.match(body[:200])
            if m:
                self.encoding = m.group(1).decode("ascii").lower()
        try:
            codecs.lookup(self.encoding)
            self.text = body.decode(self.encoding)
        except (LookupError, UnicodeDecodeError) as e:
            raise EditError(f"{name}: cannot decode as {self.encoding}: {type(e).__name__}") from None
        # 왜: 원래 바이트로 되돌아오지 않는 인코딩이면 한 글자만 바꿔도 다른 줄의 바이트가 달라질 수 있다
        if self.text.encode(self.encoding) != body:
            raise EditError(f"{name}: {self.encoding} does not round-trip byte for byte; not edited")

    def encode(self, text: str) -> bytes:
        return self.bom + text.encode(self.encoding)


def _split_lines(text: str) -> list[str]:
    """``\\n`` 에서만 나눈다(줄 끝 글자 유지). ``str.splitlines`` 는 \\x0c 등에서도 나눠 줄 번호가 Maven/편집기와 달라진다."""
    parts = text.split("\n")
    lines = [p + "\n" for p in parts[:-1]]
    if parts[-1]:
        lines.append(parts[-1])
    return lines


def _safe_path(project: Path, pom: str) -> Path:
    """프로젝트 안의 일반 파일 경로. 절대 경로·``..``·역슬래시·심볼릭 링크(어느 단계든)·프로젝트 밖으로 나가는 경로는 거부."""
    if not pom or "\\" in pom or "\x00" in pom:
        raise EditError(f"invalid pom path {pom!r}")
    pp = PurePosixPath(pom)
    if pp.is_absolute() or re.match(r"^[A-Za-z]:", pom) or any(part in ("..", "") for part in pom.split("/")):
        raise EditError(f"pom path {pom!r} is not a plain relative path inside the project")
    base = Path(project)
    cur = base
    for part in pp.parts:
        if part == ".":
            continue
        cur = cur / part
        if cur.is_symlink():
            raise EditError(f"pom path {pom!r} goes through a symbolic link")
    try:
        cur.resolve().relative_to(base.resolve())
    except ValueError:
        raise EditError(f"pom path {pom!r} escapes the project") from None
    if not cur.is_file():
        raise EditError(f"pom {pom!r} does not exist")
    return cur


def _check_edit_shape(e: TextEdit) -> None:
    if not isinstance(e.line, int) or e.line < 1:
        raise EditError(f"{e.pom}: invalid line {e.line!r}")
    for label, value in (("old", e.old), ("new", e.new)):
        if not is_version_text(value or ""):
            raise EditError(f"{e.pom}:{e.line}: {label} text {value!r} is not plain version text")
    if e.old == e.new:
        raise EditError(f"{e.pom}:{e.line}: old and new are the same ({e.old!r})")


def _apply_to_text(text: str, edits: list[TextEdit], name: str) -> str:
    """한 파일의 글자에 edit 들을 적용한 새 글자. 각 줄에서 ``old`` 가 정확히 한 번이어야 한다."""
    lines = _split_lines(text)
    by_line: dict[int, TextEdit] = {}
    for e in edits:
        _check_edit_shape(e)
        prev = by_line.get(e.line)
        if prev is not None and (prev.old, prev.new) != (e.old, e.new):
            raise EditError(f"{name}:{e.line}: two different edits on one line")
        by_line[e.line] = e
    for n, e in by_line.items():
        if n > len(lines):
            raise EditError(f"{name}:{n}: line does not exist ({len(lines)} lines)")
        line = lines[n - 1]
        count = line.count(e.old)
        if count != 1:
            raise EditError(f"{name}:{n}: {e.old!r} occurs {count} times on the line (expected exactly once)")
        lines[n - 1] = line.replace(e.old, e.new, 1)
    return "".join(lines)


def _unified(pom: str, before: str, after: str) -> str:
    """한 파일의 통합 diff(경로 a/<pom> / b/<pom>). 마지막 줄바꿈이 없는 줄은 diff 관례대로 표시한다."""
    out: list[str] = []
    for chunk in difflib.unified_diff(_split_lines(before), _split_lines(after), fromfile=f"a/{pom}", tofile=f"b/{pom}"):
        if chunk.endswith("\n"):
            out.append(chunk)
        else:
            out.append(chunk + "\n\\ No newline at end of file\n")
    return "".join(out)


def _group(edits: list[TextEdit]) -> dict[str, list[TextEdit]]:
    grouped: dict[str, list[TextEdit]] = {}
    for e in edits:
        grouped.setdefault(e.pom, []).append(e)
    return grouped


def render_diff(project: Path, edits: list[TextEdit]) -> str:
    """edit 들을 적용했을 때 나올 통합 diff(파일은 쓰지 않는다). 잘못된 edit 이면 ``EditError``. pom 경로 순으로 이어 붙인다.

    Pure: the unified diff the edits would produce (files sorted by path); raises `EditError` on an invalid edit."""
    out: list[str] = []
    for pom, group in sorted(_group(list(edits)).items()):
        path = _safe_path(Path(project), pom)
        doc = _PomText(path.read_bytes(), pom)
        out.append(_unified(pom, doc.text, _apply_to_text(doc.text, group, pom)))
    return "".join(out)


# ---------------------------------------------------------------------------------------------------- 적용
def apply_proposals(project: Path, proposals: list[Proposal]) -> ApplyResult:
    """후보 사본의 프로젝트 폴더에 제안들의 edit 을 적용한다. 모든 edit 을 먼저 검사하고(경로·줄·정확히 한 번·버전 글자·같은
    줄의 서로 다른 edit), 잘못된 제안은 이유와 함께 ``skipped`` 에 넣어 통째로 건너뛴다. ``candidate_tree_hash`` 는 그래프가 채운다.

    Validate every edit first, skip an invalid proposal whole, then apply the rest as line-based text replacements."""
    project = Path(project)
    result = ApplyResult()
    docs: dict[str, _PomText] = {}
    accepted: dict[tuple[str, int], TextEdit] = {}      # (pom, line) → edit already accepted from an earlier proposal
    chosen: list[Proposal] = []
    for p in proposals:
        try:
            if not p.edits:
                raise EditError("proposal has no edits")
            mine: dict[tuple[str, int], TextEdit] = {}
            for e in p.edits:
                _check_edit_shape(e)
                key = (e.pom, e.line)
                for seen in (accepted.get(key), mine.get(key)):
                    # 왜: 같은 줄을 서로 다르게 고치는 두 edit 은 어느 쪽이 맞는지 알 수 없다 — 뒤의 제안을 건너뛴다
                    if seen is not None and (seen.old, seen.new) != (e.old, e.new):
                        raise EditError(f"{e.pom}:{e.line}: conflicts with another edit on the same line")
                mine[key] = e
            # 이 제안까지 받아들였을 때 파일마다 글자 치환이 성립하는지 미리 확인한다(쓰기 전에)
            trial = dict(accepted)
            trial.update(mine)
            for pom in {k[0] for k in mine}:
                if pom not in docs:
                    docs[pom] = _PomText(_safe_path(project, pom).read_bytes(), pom)
                _apply_to_text(docs[pom].text, [e for (f, _n), e in trial.items() if f == pom], pom)
        except (EditError, OSError) as err:
            result.skipped[p.id] = str(err)
            log.warning("remediation apply: proposal %s skipped: %s", p.id, err)
            continue
        accepted.update(mine)
        chosen.append(p)
    patch: list[str] = []
    for pom in sorted({k[0] for k in accepted}):
        doc = docs[pom]
        new_text = _apply_to_text(doc.text, [e for (f, _n), e in accepted.items() if f == pom], pom)
        path = _safe_path(project, pom)
        _write_bytes(path, doc.encode(new_text))
        patch.append(_unified(pom, doc.text, new_text))
        result.changed_files.append(pom)
    result.applied = [p.id for p in chosen]
    result.patch = "".join(patch)
    return result


def _write_bytes(path: Path, data: bytes) -> None:
    """같은 폴더의 임시 파일 → ``os.replace``: 반쯤 쓰인 pom 이 남지 않게. 권한 비트는 원래 파일의 것을 유지한다."""
    tmp = path.with_name(f".{path.name}.sbmig-tmp")
    mode = path.stat().st_mode
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, mode & 0o7777)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------------------------------- 검증: 글자만
def _all_edits(proposals: list[Proposal]) -> dict[str, list[TextEdit]]:
    seen: dict[tuple[str, int, str, str], TextEdit] = {}
    for p in proposals:
        for e in p.edits:
            seen.setdefault((e.pom, e.line, e.old, e.new), e)
    return _group(list(seen.values()))


def _xml_shape(data: bytes) -> list[tuple]:
    """XML 노드 순서(태그·속성·tail)와 각 요소의 글자. 비교는 ``verify_text_only`` 가 한다."""
    from lxml import etree
    parser = etree.XMLParser(resolve_entities=False, no_network=True, remove_blank_text=False, huge_tree=False)
    root = etree.fromstring(data, parser=parser)
    out: list[tuple] = []
    for el in root.iter():
        tag = el.tag if isinstance(el.tag, str) else f"<{type(el).__name__}>"
        parent = el.getparent()
        ptag = parent.tag if parent is not None and isinstance(parent.tag, str) else ""
        out.append((tag, tuple(sorted((str(k), str(v)) for k, v in el.attrib.items())) if isinstance(el.tag, str) else (),
                    el.text, el.tail, ptag))
    return out


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def verify_text_only(original: Path, candidate: Path, proposals: list[Proposal], paths: list[str]) -> list[str]:
    """후보가 원본과 "계획한 버전 글자 치환" 만큼만 다른지 확인한다. 위반 문장 목록(비어 있으면 통과).
    ``original`` / ``candidate`` 는 프로젝트 폴더, ``paths`` 는 그 기준 상대 경로(원본 파일 목록). edit 을 받지 않은 파일은 바이트가
    같아야 하고, edit 을 받은 pom 은 원본에 edit 을 메모리에서 적용한 바이트와 같아야 하며 XML 요소 구조(태그 순서·속성)도 같아야 한다.

    Violations (empty = fine): untouched files byte-identical, edited poms equal to the original with exactly the planned
    replacements, same XML element structure; no file of `paths` missing on either side."""
    original, candidate = Path(original), Path(candidate)
    problems: list[str] = []
    edits = _all_edits(proposals)
    path_set = set(paths)
    for pom in sorted(edits):
        if pom not in path_set:
            problems.append(f"edited file {pom} is not in the original file list")
    for rel in sorted(path_set):
        a, b = original / rel, candidate / rel
        if not a.is_file() or a.is_symlink():
            problems.append(f"{rel}: missing in the original tree")
            continue
        if not b.is_file() or b.is_symlink():
            problems.append(f"{rel}: missing in the candidate")
            continue
        try:
            before, after = a.read_bytes(), b.read_bytes()
        except OSError as e:
            problems.append(f"{rel}: unreadable ({type(e).__name__})")
            continue
        if rel not in edits:
            if before != after:
                problems.append(f"{rel}: changed but no planned edit names it")
            continue
        try:
            doc = _PomText(before, rel)
            expected = doc.encode(_apply_to_text(doc.text, edits[rel], rel))
        except EditError as e:
            problems.append(f"{rel}: planned edits do not apply to the original ({e})")
            continue
        if expected != after:
            problems.append(f"{rel}: differs from the original by more than the planned version replacements")
            continue
        problems.extend(_structure_problems(rel, before, after))
    return problems


def _structure_problems(rel: str, before: bytes, after: bytes) -> list[str]:
    """두 pom 의 XML 구조 비교: 태그 순서·속성·tail 은 같아야 하고, 글자가 달라진 요소는 ``<version>`` 이거나
    ``<properties>`` 의 자식이어야 한다."""
    try:
        sa, sb = _xml_shape(before), _xml_shape(after)
    except Exception as e:  # noqa: BLE001 - any parse failure is a violation, never a pass
        return [f"{rel}: XML could not be parsed for the structure check ({type(e).__name__})"]
    if len(sa) != len(sb) or any(x[0] != y[0] or x[1] != y[1] or x[4] != y[4] for x, y in zip(sa, sb)):
        return [f"{rel}: XML element structure changed"]
    out: list[str] = []
    for x, y in zip(sa, sb):
        if x[3] != y[3]:
            out.append(f"{rel}: text between elements changed after <{_local(x[0])}>")
        elif x[2] != y[2] and not (_local(x[0]) == "version" or _local(x[4]) == "properties"):
            out.append(f"{rel}: text of <{_local(x[0])}> changed (only <version> / property values may change)")
    return out


# ---------------------------------------------------------------------------------------------------- 검증: 해석 결과
def _module_map(model: ResolvedModel) -> dict[str, dict[str, set[str]]]:
    """모듈 dir → g:a → 해석된 버전들."""
    out: dict[str, dict[str, set[str]]] = {}
    for m in model.modules:
        deps = out.setdefault(m.dir, {})
        for d in m.dependencies:
            deps.setdefault(d.ga, set()).add(d.version)
    return out


def _direct_gas(model: ResolvedModel) -> set[str]:
    return {d.ga for m in model.modules for d in m.dependencies if d.direct}


def verify_resolution(proposals: list[Proposal], before: ResolvedModel, after: ResolvedModel,
                      packaged: list[PackagedLib]) -> list[str]:
    """적용한 제안이 Maven 해석·패키징 결과에 계획대로 나타났는지. 문제 문장 목록 — ``info:`` 로 시작하는 줄은 참고용(막지 않는다).
    규칙: 제안의 ga 는 어느 모듈에서도 ``from_version`` 으로 해석되지 않고, 전에 ``from_version`` 이던 모듈에서는 ``to_version`` 이어야
    한다. 제안이 다루지 않은 아티팩트의 버전 변화는 전(before)에 **직접** 의존성이었을 때만 문제다(속성·부모·BOM 제안과 함께 움직인
    것과 전이 의존성은 info). ``after.complete`` False 는 문제. 패키징된 jar 에 ``from_version`` 이 남아 있으면 문제.

    Problems (lines starting with `info:` are informational): the proposal's ga moved off `from_version` to
    `to_version` wherever it resolved at `from_version`; uncovered version changes are problems only for direct
    dependencies of `before`; an incomplete `after` is a problem; no packaged jar may still carry `from_version`."""
    problems: list[str] = []
    if not after.complete:
        problems.append("resolution after the change is incomplete: " + "; ".join(after.notes or ["(no detail)"]))
    if not before.complete:
        problems.append("resolution before the change is incomplete: " + "; ".join(before.notes or ["(no detail)"]))
    bmap, amap = _module_map(before), _module_map(after)
    covered = {p.ga for p in proposals}
    for p in proposals:
        if p.from_version in after.versions_of(p.ga):
            problems.append(f"{p.ga} still resolves at {p.from_version} ({p.id})")
        for mdir, deps in sorted(bmap.items()):
            if p.from_version not in deps.get(p.ga, set()):
                continue
            if mdir not in amap:
                problems.append(f"module {mdir} is missing from the resolution after the change ({p.id})")
            elif p.to_version not in amap[mdir].get(p.ga, set()):
                got = ", ".join(sorted(amap[mdir].get(p.ga, set()))) or "nothing"
                problems.append(f"{p.ga} in module {mdir} resolves at {got}, planned {p.to_version} ({p.id})")
    direct = _direct_gas(before)
    shared = [p for p in proposals if p.kind in _SHARED_KINDS]
    broad = any(p.kind in (K_PARENT_VERSION, K_BOM_VERSION) for p in proposals)
    for mdir in sorted(set(bmap) | set(amap)):
        b, a = bmap.get(mdir, {}), amap.get(mdir, {})
        for ga in sorted(set(b) | set(a)):
            if ga in covered or b.get(ga, set()) == a.get(ga, set()):
                continue
            was = ", ".join(sorted(b.get(ga, set()))) or "absent"
            now = ", ".join(sorted(a.get(ga, set()))) or "absent"
            line = f"{ga} in module {mdir} changed {was} → {now}"
            if ga not in direct:
                problems.append(f"info: {line} (transitive)")
                continue
            # 왜: 같은 속성 값(from → to)으로 함께 움직인 직접 의존성, 또는 부모·BOM 버전 변경으로 움직인 것은 예상된 변화다
            if broad or any(p.from_version in b.get(ga, set()) and p.to_version in a.get(ga, set()) for p in shared):
                problems.append(f"info: {line} (moved with a property / parent / BOM change)")
            else:
                problems.append(f"{line} without a proposal covering it")
    for p in proposals:
        artifact = p.ga.split(":", 1)[1] if ":" in p.ga else p.ga
        stale_name = f"{artifact}-{p.from_version}.jar"
        for lib in packaged:
            if lib.ga == p.ga and lib.version == p.from_version:
                problems.append(f"{lib.artifact} still packages {lib.entry} ({p.ga}:{p.from_version}, {p.id})")
            elif (not lib.ga or (lib.ga == p.ga and not lib.version)) and lib.file_name == stale_name:
                problems.append(f"{lib.artifact} still packages {lib.entry} ({stale_name}, {p.id})")
    return problems
