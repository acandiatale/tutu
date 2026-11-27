"""올린 파일 이름으로 생성할 프로젝트의 이름, 도메인, 기본 패키지를 정하는 모듈.

Contract: docs/FW-CONVERT.md section 3.2 and scenario A13. Pure string work, no I/O.

Rules decided here where the specification is silent:
- Archive extensions are removed repeatedly from the end (``x.war.zip`` loses both), case ignored.
- At most one prefix and then at most one suffix are removed, case ignored, the longest match of each list wins.
- A project name keeps ASCII letters, digits and ``_`` only. A segment that starts with a digit gets ``x`` in front,
  a Java keyword (``_`` included) gets ``_`` at the end. The same repair applies to every package segment.
- Empty name: when removing the prefix / suffix leaves nothing usable, the name before the removal is used
  (``sample-.zip`` gives ``sampleProject``); when that is empty too (only symbols), ``FALLBACK_NAME`` is used.
- An override that cleans to nothing is ignored and the file name decides (``source`` stays ``file``).
- Domain: the given one when it is in ``naming.domains`` (case ignored), else ``naming.default_domain``; both are
  stored in lower case and repaired like any segment. An empty domain leaves its package segment out."""
from __future__ import annotations

import re

from sbmig.fwconvert.model import ModuleNames, ProjectIdentity
from sbmig.fwconvert.naming import JAVA_KEYWORDS, camel
from sbmig.fwconvert.rules import ConvertRules

# 이름에서 떼는 압축 파일 확장자. 긴 것부터 비교한다
ARCHIVE_EXTENSIONS = (".tar.gz", ".tgz", ".zip", ".war", ".jar")
# 이름을 만들 수 없을 때 쓰는 이름. 뒤에 project_suffix가 붙는다
FALLBACK_NAME = "app"

_NOT_IDENT = re.compile(r"[^A-Za-z0-9_]")


def strip_archive_name(file_name: str) -> str:
    """파일 이름에서 앞의 경로와 압축 파일 확장자를 뗀 이름을 반환한다.
    ``/``와 ``\\``를 모두 경로 구분자로 본다. 확장자가 여러 개 붙어 있으면 모두 뗀다."""
    name = re.split(r"[\\/]", str(file_name or "").strip())[-1].strip()
    changed = True
    while changed:
        changed = False
        low = name.lower()
        for ext in ARCHIVE_EXTENSIONS:
            if low.endswith(ext):
                name = name[: len(name) - len(ext)]
                changed = True
                break
    return name


def java_segment(text: str) -> str:
    """문자열을 자바 패키지 조각으로 쓸 수 있게 고친다. 쓸 수 없는 문자는 뺀다.
    숫자로 시작하면 앞에 ``x``를 붙이고, 예약어면 끝에 ``_``를 붙인다.
    영문자나 숫자가 하나도 없으면 빈 문자열을 반환한다."""
    s = _NOT_IDENT.sub("", str(text or ""))
    if not re.search(r"[A-Za-z0-9]", s):
        return ""
    if s[0].isdigit():
        s = "x" + s
    if s in JAVA_KEYWORDS or s == "_":
        s += "_"
    return s


def _strip_one(name: str, candidates: list[str], *, prefix: bool) -> str:
    low = name.lower()
    hits = [c for c in candidates if c and (low.startswith(c.lower()) if prefix else low.endswith(c.lower()))]
    if not hits:
        return name
    # 여러 개가 맞으면 가장 긴 것 하나만 뗀다. 길이가 같으면 소문자 순서로 정한다
    best = min(hits, key=lambda c: (-len(c), c.lower()))
    return name[len(best):] if prefix else name[: len(name) - len(best)]


def project_base_name(file_name: str, rules: ConvertRules) -> str:
    """올린 파일 이름으로 프로젝트 이름의 앞부분을 만든다. ``project_suffix``는 붙이지 않는다.
    접두사와 접미사를 하나씩 뗀 뒤 camelCase로 바꾼다.
    떼고 나서 남는 것이 없으면 떼기 전 이름을 쓰고, 그것도 없으면 ``FALLBACK_NAME``을 쓴다."""
    stem = strip_archive_name(file_name)
    cut = _strip_one(stem, rules.strs("naming.name_prefixes"), prefix=True)
    cut = _strip_one(cut, rules.strs("naming.name_suffixes"), prefix=False)
    for candidate in (cut, stem):
        name = java_segment(camel(candidate))
        if name:
            return name
    return FALLBACK_NAME


def _domain(domain: str, rules: ConvertRules) -> str:
    want = str(domain or "").strip().lower()
    allowed = {d.strip().lower() for d in rules.strs("naming.domains") if d.strip()}
    chosen = want if want and want in allowed else rules.text("naming.default_domain").strip().lower()
    return java_segment(chosen) if chosen else ""


def derive_identity(file_name: str, rules: ConvertRules, *, domain: str = "", override: str = "") -> ProjectIdentity:
    """생성할 프로젝트의 이름, 도메인, 기본 패키지, 모듈 이름을 정해 반환한다.
    ``override``가 있으면 그 이름을 접미사 없이 그대로 쓴다. 자바 식별자로 쓸 수 없는 문자만 고친다.
    ``override``를 고친 결과가 비어 있으면 파일 이름으로 정한다.
    ``domain``이 ``naming.domains``에 없으면 ``naming.default_domain``을 쓴다. 그것도 비어 있으면 패키지에 도메인을 넣지 않는다."""
    project = java_segment(str(override or "").strip())
    source = "override"
    if not project:
        suffix = rules.text("naming.project_suffix")
        project = java_segment(project_base_name(file_name, rules) + suffix) or FALLBACK_NAME
        source = "file"
    dom = _domain(domain, rules)
    root = [java_segment(s) for s in rules.text("naming.package_root").split(".")]
    segments = [s for s in root if s] + ([dom] if dom else []) + [project]
    modules = ModuleNames(boot=f"{project}-boot", domain=f"{project}-domain",
                          service=f"{project}-service", store=f"{project}-store")
    return ProjectIdentity(project_name=project, domain=dom, package_base=".".join(segments),
                           modules=modules, source=source)
