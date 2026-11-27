"""소스가 MS949나 EUC-KR인 모듈 pom에 ``project.build.sourceEncoding``을 기록하는 모듈.
선언 없이 한글 소스를 쓰던 모듈이 JDK 21에서 깨지지 않게 한다.

Callable with or without the restructure context. Idempotent: a module whose pom declares the matching
encoding is no longer a mismatch. Reduced runs write it BEFORE OpenRewrite (it skips a file it cannot decode).

javac's default encoding is UTF-8 since JDK 18, so such a module without a declaration breaks on JDK 21.
Callers: the restructure (``LegacyContextMixin._lc_source_encodings``) and the orchestrator of a reduced run.
A module that mixes UTF-8 with another encoding, a module whose compiler-plugin ``<encoding>`` says
otherwise, and every module while ``layout.legacy_context.write_source_encoding`` is off get a hand-over
sentence only. A value set by a parent pom outside the project is checked only when the as-is effective
pom capture is given.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from .pom_editor import PomEditError, PomEditor

NOTE_PREFIX = "source encoding written: "


@dataclass
class EncodingWrite:
    """소스 인코딩 단계의 결과를 담는 클래스. 기록한 줄, 메모, 인수인계 문장, 저장한 pom을 담는다.

    `written` lines: "<pom>: project.build.sourceEncoding=MS949 (n of m sampled .java files cp949)";
    `notes` = the same with the `source encoding written: ` prefix."""
    written: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    followups: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)


def inherited_encoding(pom: Any, poms: list, capture: Optional[dict]) -> tuple[Optional[str], str]:
    """프로젝트 밖 부모 pom이 모듈에 주는 ``project.build.sourceEncoding``을 (값, 부모 좌표)로 반환한다.
    판단할 수 없으면 (None, "")을 반환한다.

    Read from the as-is effective pom capture (`logs/effective-depmgmt.json`), which holds the merged
    properties of the reactor root only. So the value counts only when the root pom does not declare the
    property itself (it was inherited) and the top of the module's in-project chain inherits the same
    external parent g:a."""
    from sbmig.review.inputs import ENCODING_PROPERTY, _chain

    if not isinstance(capture, dict):
        return None, ""
    value = str((capture.get("properties") or {}).get(ENCODING_PROPERTY) or "").strip()
    parent = str(capture.get("parent") or "")
    if not value or "${" in value or parent.count(":") < 1:
        return None, ""
    root = next((p for p in poms if p.path == "pom.xml"), None)
    if root is None or ENCODING_PROPERTY in (root.properties or {}):
        return None, ""
    top = _chain(pom, poms)[-1]
    pkey = ":".join(parent.split(":")[:2])
    if top.parent is None or top.parent_in_repo or top.parent.key != pkey:
        return None, ""
    return value, parent


def write_source_encodings(ws: Any, cfg: Any, *, on: bool = True, capture: Optional[dict] = None) -> EncodingWrite:
    """표본 소스가 MS949나 EUC-KR인데 맞는 선언이 없는 모듈 pom에 ``project.build.sourceEncoding``을 기록한다.
    ``capture``를 주면 프로젝트 밖 부모가 정한 값도 선언으로 본다. 맞으면 그대로 두고, 다르면 인수인계만 남긴다.
    pom 저장에서 난 OSError만 밖으로 던진다. 읽을 수 없는 pom은 건너뛴다.

    `capture` = the as-is effective pom (reduced runs, where the external parent is kept)."""
    from sbmig.analyze.pom_parser import parse_poms
    from sbmig.review.inputs import ENCODING_PROPERTY, compatible, java_charset, source_encodings

    out = EncodingWrite()
    poms, _warn = parse_poms(ws)
    encs = source_encodings(ws, poms, getattr(cfg.build, "encoding_candidates", None),
                            skip_tests=bool(getattr(cfg.build, "skip_test_compile", True)))
    by_path = {p.path: p for p in poms}

    def followup(text: str) -> None:
        if text not in out.followups:
            out.followups.append(text)

    for me in encs:
        bad = me.mismatch
        if not bad:
            continue
        enc = bad[0]
        charset = java_charset(enc)
        if me.mixed:
            followup(f"{me.pom}: the sources mix {charset} and UTF-8 files ({me.counts}) — convert one side; "
                     "sbmig wrote no project.build.sourceEncoding")
            continue
        if capture is not None and not me.declared and me.pom in by_path:
            value, parent = inherited_encoding(by_path[me.pom], poms, capture)
            if value and compatible(enc, value):
                continue                                   # 유지하는 부모가 이미 이 인코딩으로 읽는다
            if value:
                followup(f"{me.pom}: the sources are {charset} but the parent {parent} sets {ENCODING_PROPERTY}="
                         f"{value} — set it to {charset} in {me.pom}; sbmig keeps the parent's value")
                continue
        if not on:
            followup(f"{me.pom}: the sources are {charset} but no project.build.sourceEncoding says so "
                     "(layout.legacy_context.write_source_encoding: false) — set it to " + charset)
            continue
        if "maven-compiler-plugin" in (me.declared_in or ""):
            followup(f"{me.pom}: the sources are {charset} but {me.declared_in} says {me.declared} — set it to "
                     f"{charset}")
            continue
        try:
            ed = PomEditor(ws.abs(me.pom))
        except PomEditError:
            continue
        ed.set_property(ENCODING_PROPERTY, charset)
        if ed.changed:
            ed.save()
            out.changed.append(ws.rel(ed.path))
            n = me.counts.get(enc, 0)
            line = f"{me.pom}: {ENCODING_PROPERTY}={charset} ({n} of {me.sampled} sampled .java files {enc})"
            out.written.append(line)
            out.notes.append(NOTE_PREFIX + line)
    return out


__all__ = ["NOTE_PREFIX", "EncodingWrite", "inherited_encoding", "write_source_encodings"]
