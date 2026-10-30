"""구조 검토가 LLM 에 보여 주는 사실을 모은다(읽기 전용 — 워크스페이스를 바꾸지 않는다).
흐름상 위치: `restructure.review` 에서 StructureReviewer 가
`collect_review_input(ws, profile, cfg, notes=…, results=…)`
를 부르고, `ReviewInput.render()` 가 user 프롬프트가 된다. 담는 것: 모든 pom(≤ `review.max_poms`, 합계 ≤
`review.max_pom_chars`, 긴 pom 은 앞 + 뒤만), 모듈 트리(packaging·parent·누락 / 미등록 모듈), 재구성이 한 일,
config 의 회사 규칙(`artifacts.*`, `layout.*`), 프로필의 케이스 종류·버전, Maven 없이 보이는 사실(필수 / 선택
사내 아티팩트, javax / jakarta API 선언, 중복 선언, 필터링되는 바이너리 리소스), 모듈별 소스 인코딩(.java 표본
vs 선언된 `project.build.sourceEncoding`). 지킬 것: 회사 규칙은 config 에서만 읽는다(코드에 박지 않음,
불변조건 2). 인코딩 불일치는 빌드 오류로 절대 드러나지 않으므로(JDK 21 javac 는 경고만 내고 한글 리터럴이
깨진다) 여기서 사실로 계산해 준다.

What the structure review shows the LLM (v2, docs/v2/PLAN-v2.md §3.3).

`collect_review_input(ws, profile, cfg, notes=…, results=…)` gathers, after the restructure and before the first
build: every pom of the workspace (≤ `review.max_poms`, all text ≤ `review.max_pom_chars`, long poms cut head + tail),
the module tree (packaging, parent, listed / missing / unlisted modules), what the restructure did (notes +
`RestructureResult` fields), the company rules taken from the config (`artifacts.*`, `layout.*` — never from code,
invariant 2), the case type and versions from the profile, a few facts sbmig can see without Maven (mandatory /
optional artifacts, javax / jakarta API declarations, duplicate declarations) and the source encodings per module
(sampled `.java` files vs the declared `project.build.sourceEncoding`). `ReviewInput.render()` is the user prompt.
Read-only: nothing here changes the workspace.


"""
from __future__ import annotations

import codecs
import logging
import posixpath
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from sbmig.core.models import GAV, CaseType, PomInfo, ProjectProfile
from sbmig.core.workspace import detect_encoding

log = logging.getLogger("sbmig.review")

COMPILER_PLUGIN = "maven-compiler-plugin"
ENCODING_PROPERTY = "project.build.sourceEncoding"
ENCODING_SAMPLE_TOTAL = 1500          # .java files read for the encoding facts …
ENCODING_SAMPLE_PER_MODULE = 400      # … at most this many per module (evenly spread)
ENCODING_EXAMPLES = 3                 # non-UTF-8 example files named per module and encoding
MIN_CUT_CHARS = 240                   # below this a pom is not shown at all (only its path and size)
SECTION_LINES_MAX = 60                # lines per restructure-result list / notes block
LINE_CHARS_MAX = 300
_UTF8_FAMILY = {"ascii", "utf-8", "utf-8-sig"}
# python codec → Java charset name accepted by javac / Maven (`project.build.sourceEncoding`, compiler `<encoding>`)
JAVA_CHARSETS = {"cp949": "MS949", "euc_kr": "EUC-KR", "shift_jis": "Shift_JIS", "cp932": "MS932",
                 "latin_1": "ISO-8859-1", "iso8859-1": "ISO-8859-1", "cp1252": "windows-1252", "utf-8": "UTF-8",
                 "gb2312": "GB2312", "gbk": "GBK", "big5": "Big5"}
# encodings javac reads the same way for Korean sources (EUC-KR files decode as cp949: it is a superset)
_FAMILIES = {"cp949": "korean", "euc_kr": "korean"}
_JAVA_ALIASES = {"ms949": "cp949", "x-windows-949": "cp949", "windows-949": "cp949", "windows949": "cp949",
                 "ms_949": "cp949", "uhc": "cp949", "ksc5601": "euc_kr", "ks_c_5601-1987": "euc_kr"}
_PROP_REF = re.compile(r"^\$\{([^}]+)\}$")
_JAVAX_API_ARTIFACTS = {"servlet-api", "jsp-api", "jstl", "jaxb-api", "activation", "mail", "persistence-api",
                        "validation-api", "jta", "el-api", "jaxws-api", "jsr250-api", "ejb-api", "jms"}


def codec_name(enc: Optional[str]) -> str:
    """Python / Java 식으로 쓴 인코딩 이름을 Python 코덱 이름으로
    정규화(MS949 → cp949 등; 비어 있으면 "", 모르면 소문자 그대로).

    Normalized python codec name of an encoding as Python or Java spell it ("" when unknown)."""
    if not enc:
        return ""
    s = str(enc).strip().lower()
    s = _JAVA_ALIASES.get(s, s)
    try:
        return codecs.lookup(s).name
    except LookupError:
        return s


def java_charset(enc: str) -> str:
    """감지된(Python) 인코딩의 Java charset 이름 — pom 의 `project.build.sourceEncoding` 에 적을 값.

    Java charset name for a detected (python) encoding: cp949 → MS949, euc_kr → EUC-KR, …"""
    c = codec_name(enc)
    return JAVA_CHARSETS.get(c, JAVA_CHARSETS.get(enc.lower(), enc.upper()))


def is_utf8(enc: Optional[str]) -> bool:
    """UTF-8 계열(ascii / utf-8 / utf-8-sig)인가."""
    c = codec_name(enc)
    return c in _UTF8_FAMILY or c.replace("_", "-") in _UTF8_FAMILY or c in ("utf_8", "utf_8_sig")


def compatible(detected: str, declared: Optional[str]) -> bool:
    """`declared` 로 선언된 모듈(None = UTF-8, JDK 21·부모 기본값)이 `detected` 인코딩 소스를 제대로 읽는가.
    EUC-KR 과 MS949 는 같은 한국어 계열로 본다(cp949 가 상위 집합).

    Does a module declared as `declared` (None = UTF-8, the JDK 21 / parent default) read `detected` sources?"""
    if is_utf8(detected):
        return True
    if not declared or is_utf8(declared):
        return False
    d, s = codec_name(detected), codec_name(declared)
    return d == s or (_FAMILIES.get(d) is not None and _FAMILIES.get(d) == _FAMILIES.get(s))


def _cap(text: object, limit: int = LINE_CHARS_MAX) -> str:
    s = " ".join(str(text if text is not None else "").split())
    return s if len(s) <= limit else s[: limit - 1] + "…"


# ---------------------------------------------------------------------------- poms
@dataclass
class PomText:
    """프롬프트에 보여 줄 pom 하나(잘렸으면 `cut`, 원래 길이 `chars`)."""
    path: str
    text: str                 # as shown in the prompt (cut when `cut`)
    chars: int                # original length
    cut: bool = False


def allot(lengths: list[int], total: int) -> list[int]:
    """pom 별 글자 수 배분: 합이 `total` 을 넘지 않게 짧은 pom 은 통째로, 나머지는 남은 몫을 똑같이 나눈다.

    Characters per pom so that the sum stays ≤ `total`: short poms whole, the rest share what is left equally."""
    caps = [0] * len(lengths)
    remaining = max(0, int(total))
    order = sorted(range(len(lengths)), key=lambda i: lengths[i])
    for k, i in enumerate(order):
        share = remaining // (len(lengths) - k)
        caps[i] = min(lengths[i], share)
        remaining -= caps[i]
    return caps


def cut_text(text: str, cap: int) -> tuple[str, bool]:
    """`cap` 안에 들어가면 그대로, 아니면 앞(약 2/3) + 표시 줄 + 뒤를 줄 경계에서 자른다(표시 포함 `cap`
    이하). 남는 자리가 MIN_CUT_CHARS 보다 작으면 "보여 주지 않음" 표시만 남긴다.

    `text` when it fits in `cap` characters, else head (≈ 2/3) + a marker line + tail, cut at line breaks; the
    result (marker included) is never longer than `cap`. Below MIN_CUT_CHARS only the marker is left."""
    if len(text) <= cap:
        return text, False
    marker = f"<!-- … sbmig: {{n}} of {len(text)} characters cut here (prompt budget) … -->"
    room = cap - len(marker.format(n=len(text))) - 2
    if room < MIN_CUT_CHARS:
        m = f"<!-- sbmig: pom of {len(text)} characters not shown (prompt budget) -->"
        return (m if len(m) <= cap else ""), True
    head_n = room * 2 // 3
    head = text[:head_n]
    if "\n" in head:
        head = head[: head.rindex("\n") + 1]
    tail = text[len(text) - (room - len(head)):] if room - len(head) > 0 else ""
    if "\n" in tail:
        tail = tail[tail.index("\n") + 1:]
    cut = len(text) - len(head) - len(tail)
    return f"{head.rstrip(chr(10))}\n{marker.format(n=cut)}\n{tail}", True


def module_pom_path(aggregator: str, module: str) -> str:
    """집계 pom 의 `<module>` 값 → 그 모듈 pom 의 경로(프로젝트
    루트 기준 posix, `.xml` 로 끝나지 않으면 `/pom.xml`)."""
    base = posixpath.dirname(aggregator)
    target = posixpath.normpath(posixpath.join(base or ".", module.strip()))
    if not target.endswith(".xml"):
        target = posixpath.join(target, "pom.xml")
    return posixpath.normpath(target)


def _gav(g: Optional[GAV]) -> str:
    return g.coordinates if g is not None else "-"


def _spec(s: Any) -> str:
    """`g:a:v` (in backticks) of an ArtifactSpec + version / type / scope remarks."""
    out = f"`{s.group_id}:{s.artifact_id}" + (f":{s.version}`" if s.version else "` (version managed)")
    extra = [x for x in (f"type {s.type}" if getattr(s, "type", None) else "",
                         f"scope {s.scope}" if getattr(s, "scope", None) else "") if x]
    return out + (f" [{', '.join(extra)}]" if extra else "")


# ---------------------------------------------------------------------------- encodings
@dataclass
class ModuleEncoding:
    """모듈 하나의 소스 인코딩 사실: .java 파일 수·표본 수·인코딩별 개수·예시 파일, 그리고 프로젝트 pom 안에서
    실제로 적용되는 소스 인코딩과 그 위치."""
    module: str                                 # pom directory ("" = project root)
    pom: str
    files: int = 0                              # .java files of the module
    sampled: int = 0
    counts: dict[str, int] = field(default_factory=dict)       # ascii / utf-8 / cp949 / …
    examples: dict[str, list[str]] = field(default_factory=dict)
    declared: Optional[str] = None              # effective source encoding inside the project poms
    declared_in: str = ""                       # "<pom> property" | "<pom> maven-compiler-plugin <encoding>"

    @property
    def legacy(self) -> list[str]:
        """감지된 UTF-8 아닌 인코딩(파일이 많은 순).

        Detected non-UTF-8 encodings, most files first."""
        return [e for e, _ in Counter({k: v for k, v in self.counts.items() if not is_utf8(k)}).most_common()]

    @property
    def mismatch(self) -> list[str]:
        """선언된 인코딩이 읽지 못하는 UTF-8 아닌 인코딩 — critical(javac 는 경고만 내고 리터럴이 깨진다).

        Non-UTF-8 encodings the declared one does not read (critical: javac only warns, literals garble)."""
        return [e for e in self.legacy if not compatible(e, self.declared)]

    @property
    def mixed(self) -> bool:
        """UTF-8 아닌 파일과 비ASCII UTF-8 파일이 한 모듈에 섞여 있는가(한쪽을 변환해야 한다)."""
        return bool(self.legacy) and any(is_utf8(k) and k != "ascii" for k in self.counts)

    def line(self, external_parent: str) -> str:
        """프롬프트 "# Source encodings" 의 한 항목(불일치면 **ENCODING MISMATCH** 표시와 예시 파일)."""
        where = f"`{self.module or '(root)'}` (`{self.pom}`)"
        if not self.files:
            return f"- {where}: no .java files"
        counts = ", ".join(f"{k} {v}" + (f" (Java charset {java_charset(k)})" if not is_utf8(k) else "")
                           for k, v in sorted(self.counts.items(), key=lambda kv: -kv[1]))
        sampled = f"{self.sampled} of {self.files}" if self.sampled < self.files else f"{self.files}"
        declared = (f"`{self.declared}` ({self.declared_in})" if self.declared else
                    f"not declared in the project poms (comes from {external_parent}; JDK 21 default UTF-8)")
        out = f"- {where}: {sampled} .java file(s) read — {counts}; source encoding: {declared}"
        if self.mismatch:
            names = ", ".join(java_charset(e) for e in self.mismatch)
            out += f". **ENCODING MISMATCH: {names} sources without a matching source encoding**"
            if self.mixed:
                out += " (the module also has UTF-8 files with non-ASCII text: mixed encodings)"
        for enc, files in self.examples.items():
            out += f"\n  - {enc} e.g. " + ", ".join(f"`{f}`" for f in files)
        return out


def _chain(pom: PomInfo, poms: list[PomInfo]) -> list[PomInfo]:
    """`pom` 과 프로젝트 안의 부모들(가까운 순): relativePath 로 찾고,
    좌표가 다르면 groupId:artifactId 로 찾는다. 순환은 끊는다.

    `pom` and its parents inside the project, nearest first."""
    by_key = {f"{p.group_id}:{p.artifact_id}": p for p in poms if p.group_id}
    by_path = {p.path: p for p in poms}
    out, cur, seen = [pom], pom, {pom.path}
    while cur.parent is not None and cur.parent_in_repo:
        nxt = None
        rel = cur.parent_relative_path
        if rel is None or rel:
            cand = module_pom_path(cur.path, rel or "..")
            nxt = by_path.get(cand)
        if nxt is None or f"{nxt.group_id}:{nxt.artifact_id}" != cur.parent.key:
            nxt = by_key.get(cur.parent.key, nxt)
        if nxt is None or nxt.path in seen:
            break
        out.append(nxt)
        seen.add(nxt.path)
        cur = nxt
    return out


def declared_encoding(pom: PomInfo, poms: list[PomInfo]) -> tuple[Optional[str], str]:
    """모듈에 실제로 적용되는 소스 인코딩: 가장 가까운 maven-compiler-plugin `<encoding>`, 없으면 가장 가까운
    `project.build.sourceEncoding`(`${prop}` 은 부모 사슬의 property 로 푼다). 선언이 없으면 (None, "").

    Effective source encoding of a module inside the project poms: the nearest maven-compiler-plugin `<encoding>`,
    else the nearest `project.build.sourceEncoding` (`${prop}` resolved along the chain). (None, "") = not declared."""
    chain = _chain(pom, poms)
    props: dict[str, str] = {}
    for p in reversed(chain):
        props.update(p.properties or {})

    def resolve(v: Optional[str]) -> Optional[str]:
        v = (v or "").strip()
        m = _PROP_REF.match(v)
        if m:
            v = (props.get(m.group(1)) or "").strip()
        return v or None

    # compiler 플러그인의 <encoding> 이 property 보다 먼저다 — javac 가 실제로 받는 값이기 때문
    for p in chain:
        for pl in p.plugins:
            if pl.artifact_id == COMPILER_PLUGIN:
                enc = resolve(pl.configuration.get("encoding") if isinstance(pl.configuration.get("encoding"), str)
                              else None)
                if enc and "${" not in enc:
                    return enc, f"`{p.path}` maven-compiler-plugin <encoding>"
    for p in chain:
        if ENCODING_PROPERTY in (p.properties or {}):
            enc = resolve(p.properties[ENCODING_PROPERTY])
            if enc and "${" not in enc:
                return enc, f"`{p.path}` property {ENCODING_PROPERTY}"
    return None, ""


def _owning(file: str, poms: list[PomInfo]) -> Optional[PomInfo]:
    best, best_len = None, -1
    for p in poms:
        d = posixpath.dirname(p.path)
        if (d == "" or file.startswith(d + "/")) and len(d) > best_len:
            best, best_len = p, len(d)
    return best


def _spread(items: list[str], n: int) -> list[str]:
    if len(items) <= n:
        return items
    step = len(items) / float(n)
    return [items[int(i * step)] for i in range(n)]


def source_encodings(ws: Any, poms: list[PomInfo], candidates: Optional[Iterable[str]] = None, *,
                     skip_tests: bool = True) -> list[ModuleEncoding]:
    """모듈별 .java 인코딩(순수 ASCII 는 `ascii`, 나머지는 `build.encoding_candidates` 로 `detect_encoding`)과
    각 모듈이 프로젝트 안에서 선언한 인코딩. 표본은 모듈당 ENCODING_SAMPLE_PER_MODULE, 전체
    ENCODING_SAMPLE_TOTAL 개까지 고르게 뽑고, 테스트 소스는 기본으로 뺀다(테스트는 빌드에서 건너뛴다).

    Encodings of the `.java` files per module (pure ASCII counted as `ascii`, others by `detect_encoding` with
    `build.encoding_candidates`) and the encoding each module declares inside the project."""
    cands = tuple(candidates) if candidates else None
    try:
        files = [f for f in ws.glob("*.java") if not (skip_tests and "/src/test/" in f"/{f}")]
    except Exception as e:  # pragma: no cover - unreadable tree
        log.debug("encoding scan skipped: %s", e)
        return []
    by_pom: dict[str, list[str]] = {}
    for f in files:
        owner = _owning(f, poms)
        if owner is not None:
            by_pom.setdefault(owner.path, []).append(f)
    out: list[ModuleEncoding] = []
    budget = ENCODING_SAMPLE_TOTAL
    for pom in poms:
        mine = sorted(by_pom.get(pom.path, []))
        if not mine and pom.packaging == "pom":
            continue
        me = ModuleEncoding(module=posixpath.dirname(pom.path), pom=pom.path, files=len(mine))
        me.declared, me.declared_in = declared_encoding(pom, poms)
        share = min(ENCODING_SAMPLE_PER_MODULE, max(0, budget))
        sample = _spread(mine, share)
        counts: Counter = Counter()
        for f in sample:
            try:
                data = ws.abs(f).read_bytes()
            except OSError:
                continue
            enc = "ascii" if data.isascii() else codec_name(detect_encoding(ws.abs(f), cands))
            if enc != "ascii" and is_utf8(enc):
                enc = "utf-8"
            counts[enc] += 1
            if not is_utf8(enc) and len(me.examples.setdefault(enc, [])) < ENCODING_EXAMPLES:
                me.examples[enc].append(f)
        me.sampled = sum(counts.values())
        me.counts = dict(counts)
        budget -= me.sampled
        out.append(me)
    return out


# ---------------------------------------------------------------------------- the input
@dataclass
class ReviewInput:
    """구조 검토 user 프롬프트의 재료 전부(섹션별 줄 목록 + pom
    본문 + 인코딩 사실). `render()` 가 프롬프트 문자열."""
    project: list[str] = field(default_factory=list)
    rules: list[str] = field(default_factory=list)
    restructure: list[str] = field(default_factory=list)
    modules: list[str] = field(default_factory=list)
    facts: list[str] = field(default_factory=list)
    encodings: list[ModuleEncoding] = field(default_factory=list)
    external_parent: str = "the company parent"
    poms: list[PomText] = field(default_factory=list)
    omitted_poms: list[str] = field(default_factory=list)
    pom_infos: list[PomInfo] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    language: str = "en"

    @property
    def pom_chars(self) -> int:
        """프롬프트에 실린 pom 글자 수 합."""
        return sum(len(p.text) for p in self.poms)

    @property
    def encoding_mismatches(self) -> list[ModuleEncoding]:
        """인코딩 불일치가 있는 모듈들(LLM 이 놓치면 검토기가 critical 로 더한다)."""
        return [m for m in self.encodings if m.mismatch]

    def render(self) -> str:
        """user 프롬프트. 섹션 순서: Project → Company rules → What the restructure did → Module tree → Facts
        → Source encodings → Poms → Your task. `report.language`
        가 ko 면 이슈 문장을 한국어로 쓰라는 줄을 더한다."""
        L: list[str] = ["# Project", ""]
        L += self.project
        L += ["", "# Company rules (sbmig configuration)", ""]
        L += self.rules
        L += ["", "# What the restructure did", ""]
        L += self.restructure or ["- (nothing recorded)"]
        L += ["", "# Module tree after the restructure", ""]
        L += self.modules
        L += ["", "# Facts sbmig collected without Maven (check them against the poms)", ""]
        L += self.facts or ["- (none)"]
        L += ["", "# Source encodings (sampled .java files per module)", ""]
        L += [m.line(self.external_parent) for m in self.encodings] or ["- no .java files found"]
        L += ["", f"# Poms ({len(self.poms)} shown, {self.pom_chars} characters)", ""]
        if self.omitted_poms:
            L += [f"Not shown (review.max_poms): {', '.join(f'`{p}`' for p in self.omitted_poms)}", ""]
        for p in self.poms:
            L.append(f"## `{p.path}`" + (f" ({p.chars} characters, cut)" if p.cut else ""))
            L += ["", "```xml", p.text.rstrip("\n"), "```", ""]
        L += ["# Your task", "",
              "Review the build structure above against the company rules and answer in the format of the system "
              "prompt: one `<review>` JSON block and — only for critical issues you can fix in the build files — "
              "one `<script>`."]
        if self.language == "ko":
            L += ["Write `problem`, `fix_hint` and `analysis` in Korean (identifiers, coordinates and paths as "
                  "they are)."]
        return "\n".join(L).rstrip() + "\n"


def _project_name(cfg: Any, poms: list[PomInfo], profile: Optional[ProjectProfile]) -> str:
    if getattr(cfg.layout, "project_name", None):
        return cfg.layout.project_name
    top = next((p for p in poms if p.path == "pom.xml"), None)
    if top is not None and top.artifact_id:
        return top.artifact_id
    rp = getattr(profile, "root_pom", None) if profile is not None else None
    return (getattr(rp, "artifact_id", None) or getattr(profile, "name", None) or "project") if profile else "project"


def project_lines(profile: Optional[ProjectProfile], cfg: Any, name: str) -> list[str]:
    """프롬프트 "# Project" 의 줄들: 이름·케이스, as-is 버전, 목표(JDK·Boot·Framework 는 `cfg.target`),
    소스·웹 리소스 수, MSA 서비스, as-is boot 모듈, 테스트 건너뜀 안내."""
    t = cfg.target
    if profile is None:
        return [f"- name `{name}`, case unknown", f"- target: JDK {t.java_version}, Spring Boot "
                f"{t.spring_boot_version}, Spring Framework {t.spring_framework_version}"]
    s = profile.source
    asis = (f"java {profile.java_version or '?'}, spring boot {profile.spring_boot_version or '-'}, spring "
            f"{profile.spring_version or '-'}, packaging {profile.packaging or '-'}, {len(profile.poms)} pom(s)")
    web = [x for x in (f"web.xml {s.web_xml_spec or ''}".strip() if s.has_web_xml else "",
                       f"{s.jsp_count} JSP(s)" if s.jsp_count else "",
                       f"{s.web_static_count} static web file(s)" if s.web_static_count else "") if x]
    out = [f"- name `{name}`, case `{profile.case_type.value}`" + (" (multi-module)" if profile.is_multi_module else ""),
           f"- as-is (before the migration): {asis}",
           f"- target: JDK {t.java_version}, Spring Boot {t.spring_boot_version}, Spring Framework "
           f"{t.spring_framework_version}, Jakarta EE (javax.* → jakarta.* already rewritten by OpenRewrite)",
           f"- sources: {s.java_files} main .java file(s), {s.test_files} test file(s)"
           + (f"; web: {', '.join(web)}" if web else "")]
    if profile.services:
        out.append("- services (MSA): " + ", ".join(f"`{x.name}` ({x.kind}, `{x.root_pom}`)"
                                                     for x in profile.services[:20]))
    if profile.boot_modules:
        out.append("- as-is boot modules: " + ", ".join(f"`{m}`" for m in profile.boot_modules))
    if cfg.build.skip_test_compile:
        out.append("- every build runs with `-Dmaven.test.skip=true`: test sources are neither compiled nor run "
                   "(test-scope problems are warnings at most)")
    return out


def rule_lines(cfg: Any, profile: Optional[ProjectProfile], name: str) -> tuple[list[str], str]:
    """이 케이스에 맞는 회사 규칙 줄들(`cfg.artifacts` / `cfg.layout` 에서 읽음)과 외부 부모를 부르는 이름.
    레이아웃은 케이스별: MSA(디렉토리 그대로, pom 만), 분리 대상(spring_legacy / servlet_tomcat / pure_java →
    `{name}-boot` / `-back` / `-front`), Boot as-is(pom 만, 단일 모듈은 `{name}-boot` 로 감쌈).

    The company rules from `cfg.artifacts` / `cfg.layout` for this case, and how to name the external parent."""
    a, lay = cfg.artifacts, cfg.layout
    case = profile.case_type if profile is not None else CaseType.UNKNOWN
    L = [f"- root pom parent (`artifacts.parent`): {_spec(a.parent)}"]
    if a.role_parents:
        L.append(f"- boot module parent (`artifacts.boot_parent`): {_spec(a.boot_parent)}")
        L.append(f"- every other module's parent (`artifacts.comp_parent`): {_spec(a.comp_parent)}; the root pom is "
                 "then an aggregator the modules do not inherit from")
        external = f"the role parents `{a.boot_parent.key}` / `{a.comp_parent.key}`"
    else:
        L.append("- no role parents configured: the modules inherit the root pom")
        external = f"the parent `{a.parent.key}`"
    if a.mandatory:
        where = ("every root pom (the project root; in MSA each service root and the top aggregator)"
                 if a.mandatory_target == "root" else "every module pom (boot / comp role modules)")
        L.append(f"- mandatory dependencies, declared in {where} (`artifacts.mandatory_target: {a.mandatory_target}`; "
                 "type pom + scope import → in dependencyManagement):")
        L += [f"  - {_spec(s)}" for s in a.mandatory]
    if a.optional:
        L.append(f"- optional in-house artifacts — only where already used, version policy "
                 f"`{a.optional_version_policy}`:")
        L += [f"  - {_spec(s)}" + (f" (aliases {', '.join(s.aliases)})" if s.aliases else "") for s in a.optional]
    if a.remove_when_present:
        L.append("- must not be declared any more (`artifacts.remove_when_present`): "
                 + ", ".join(f"`{k}`" for k in a.remove_when_present))
    # v2.1t: auto 는 재구성이 부모 체인의 pom 으로 정한다(실효값은 재구성 결과에)
    if a.parent_manages_spring_boot == "auto":
        boot_v = ("decided from the parent chain's poms (auto): managed by the chain when it reaches "
                  f"spring-boot-dependencies, else spring-boot-dependencies {cfg.target.spring_boot_version} imported "
                  "in the poms (see the restructure's BOM imports)")
    else:
        boot_v = ("managed by the parent chain" if a.parent_manages_spring_boot
                  else f"spring-boot-dependencies {cfg.target.spring_boot_version} imported in the poms")
    java_v = ("managed by the parent chain" if a.parent_manages_java_version
              else f"maven.compiler.release {cfg.target.java_version} in the poms")
    L.append(f"- Spring Boot versions: {boot_v}; Java level: {java_v}")
    boot = lay.module_path("boot", name)
    comp = lay.module_path("comp", name)
    front = lay.module_path("front", name)
    jsp = bool(profile is not None and profile.source.jsp_count)
    web = bool(profile is not None and (profile.source.jsp_count or profile.source.web_static_count))
    split = case in (CaseType.SPRING_LEGACY, CaseType.SERVLET_TOMCAT, CaseType.PURE_JAVA, CaseType.UNKNOWN) or (
        case == CaseType.SPRING_BOOT and lay.boot_as_is == "layout")
    if case == CaseType.MSA:
        L.append(f"- layout (MSA): directories untouched, poms only; every service gets the same parent / mandatory / "
                 f"optional rules (`layout.msa_parent_policy: {lay.msa_parent_policy}`); service modules named like "
                 f"`{lay.boot_module_pattern}` whose pom is a Boot app → boot parent, other modules → comp parent")
    elif split:
        pkg = "war (JSPs served by an external Tomcat)" if jsp and lay.jsp_boot_packaging == "war" else "jar"
        L.append(f"- layout (case `{case.value}`): the root is a `pom` aggregator; modules directly under it:")
        L.append(f"  - `{boot}`: Boot configuration only (application class, application.* resources), packaging "
                 f"{pkg}, parent = boot parent")
        L.append(f"  - `{comp}`: every source incl. controllers, packaging jar, parent = comp parent")
        if front and web:
            L.append(f"  - `{front}`: web resources only (static under `{lay.front_static_dir}`, JSPs under "
                     f"`{lay.front_jsp_dir}`), packaging jar, parent = comp parent")
        elif front:
            L.append(f"  - `{front}`: only when the project has web resources (none detected)")
        L.append(f"  - the boot module depends on `{comp}`" + (f" and `{front}`" if front and web else ""))
    else:
        L.append(f"- layout (Spring Boot as-is, `layout.boot_as_is: {lay.boot_as_is}`): directories are kept, only the "
                 f"poms change — root → root parent; module dirs named like `{lay.boot_module_pattern}` whose pom is a "
                 "Boot app → boot parent; every other module → comp parent; a single-module Boot app is wrapped "
                 f"into `{boot}` under an aggregator root")
    if lay.module_dir:
        L.append(f"- modules live under `{lay.module_dir}/`")
    return L, external


def _field(r: Any, key: str, default: Any = None) -> Any:
    return r.get(key, default) if isinstance(r, dict) else getattr(r, key, default)


def restructure_lines(notes: Iterable[str], results: Iterable[Any]) -> list[str]:
    """재구성이 한 일: RestructureResult(또는 그 필드의 dict)마다
    한 블록 + 실행 메모. 목록은 SECTION_LINES_MAX 줄까지.

    What the restructure did: one block per `RestructureResult` (or dict of its fields) + the run notes."""
    out: list[str] = []
    for r in results or ():
        if r is None:
            continue

        def g(key: str, default: Any = None, _r: Any = r) -> Any:
            return _field(_r, key, default)
        state = "ok" if g("ok", True) else f"FAILED: {_cap(g('error') or '?')}"
        layout = ", ".join(f"{k} `{g(k)}`" for k in ("boot_module", "comp_module", "front_module", "boot_packaging",
                                                      "kept_boot_module") if g(k))
        out.append(f"- restructure result: {state}" + (f"; {layout}" if layout else ""))
        for key, title in (("parents_set", "parents set"), ("mandatory_added", "mandatory added"),
                           ("optional_bumped", "optional bumped"),
                           # v2.1n: 트리거 의존성 때문에 새로 넣은 선택 아티팩트와 그때 지운 트리거
                           ("optional_added", "optional artifacts added for trigger dependencies"),
                           ("optional_replaced", "trigger dependencies replaced by optional artifacts"),
                           ("merged_modules", "merged modules"),
                           ("managed_versions_dropped", "explicit versions dropped (managed by the new parents)"),
                           # v2.1o: 레시피가 관리 버전보다 높여 놓은 명시 버전도 지웠다(as-is 는 관리 버전 이하)
                           ("recipe_versions_dropped",
                            "recipe-raised versions dropped (the as-is build was at or below the managed version)"),
                           # v2.1l: 검토도 재구성이 적은 as-is 버전을 다른 pom 교정처럼 본다
                           ("versions_pinned", "versions carried over from the as-is build"),
                           ("versions_unknown", "version-less dependencies the as-is build did not resolve"),
                           # v2.1o: 부모의 Boot BOM 속성을 덮어쓰던 as-is 속성을 재구성이 이미 지웠다
                           ("bom_properties_removed",
                            "as-is version properties removed (they overrode the parent's Spring Boot BOM properties)"),
                           # v2.1t: 재구성이 적은 BOM import(부모 체인 속성 참조 또는 리터럴)
                           ("bom_imports", "BOM imports written by the restructure"),
                           ("version_properties_removed",
                            "version properties removed (only the dropped explicit versions used them)"),
                           ("generated_files", "generated files"), ("flags", "flags"),
                           ("manual_followups", "follow-ups"), ("notes", "notes")):
            items = [x for x in (g(key) or []) if x]
            if not items:
                continue
            out.append(f"  - {title}:")
            out += [f"    - {_cap(x)}" for x in items[:SECTION_LINES_MAX]]
            if len(items) > SECTION_LINES_MAX:
                out.append(f"    - … {len(items) - SECTION_LINES_MAX} more")
        conflicts = g("merge_conflicts") or []
        if conflicts:
            out.append(f"  - merge conflicts (first-wins): {len(conflicts)}")
    extra = [n for n in (notes or ()) if n]
    if extra:
        out.append("- run notes:")
        out += [f"  - {_cap(n)}" for n in extra[:SECTION_LINES_MAX]]
        if len(extra) > SECTION_LINES_MAX:
            out.append(f"  - … {len(extra) - SECTION_LINES_MAX} more")
    return out


def module_lines(poms: list[PomInfo], ws: Any) -> list[str]:
    """모듈 트리 표(pom · groupId:artifactId · packaging · parent · 등록된 모듈), pom 이 없는
    `<module>`(**MISSING**), 어느 집계 pom 에도 등록되지 않은 pom 목록."""
    paths = {p.path for p in poms}
    referenced: set[str] = set()
    L = ["| pom | groupId:artifactId | packaging | parent | modules listed |", "|---|---|---|---|---|"]
    for p in poms:
        mods = []
        for m in p.modules:
            target = module_pom_path(p.path, m)
            referenced.add(target)
            exists = target in paths or _exists(ws, target)
            mods.append(f"`{m}`" + ("" if exists else " (**MISSING: no pom.xml**)"))
        parent = "-"
        if p.parent is not None:
            parent = f"`{_gav(p.parent)}`" + (" (in project)" if p.parent_in_repo else " (external)")
        L.append(f"| `{p.path}` | `{p.group_id or '?'}:{p.artifact_id}` | {p.packaging} | {parent} | "
                 f"{', '.join(mods) or '-'} |")
    unlisted = [p.path for p in poms if p.path not in referenced and p.path != "pom.xml"] \
        if any(q.modules for q in poms) else []
    if unlisted:
        L.append("")
        L.append("Poms no aggregator lists as a module: " + ", ".join(f"`{u}`" for u in unlisted))
    return L


def _exists(ws: Any, rel: str) -> bool:
    try:
        return bool(ws.exists(rel))
    except Exception:  # pragma: no cover
        return False


def _is_javax_api(g: GAV) -> bool:
    return g.group_id.startswith("javax.") or g.artifact_id.startswith("javax.") or (
        g.group_id.startswith("javax") and g.artifact_id in _JAVAX_API_ARTIFACTS)


def fact_lines(poms: list[PomInfo], cfg: Any) -> list[str]:
    """Maven 없이 sbmig 가 모은 사실(LLM 이 pom 과 대조한다): 필수 아티팩트가 선언된 곳(없으면 **not
    declared**), 쓰고 있는 선택 아티팩트, 아직 남은 `artifacts.remove_when_present`, javax.* / jakarta.* API
    의존성, 한 pom 안의 중복 선언."""
    a = cfg.artifacts
    L: list[str] = []

    def declared(key: str) -> list[str]:
        out = []
        for p in poms:
            for d in p.dependencies:
                if d.key == key:
                    out.append(f"`{p.path}` {d.version or '(managed)'}" + (f" scope {d.scope}" if d.scope else ""))
            for d in p.dependency_management:
                if d.key == key:
                    out.append(f"`{p.path}` dependencyManagement {d.version or '-'}"
                               + (f" {d.type}/{d.scope}" if d.type or d.scope else ""))
        return out

    for s in a.mandatory:
        where = declared(s.key)
        L.append(f"- mandatory `{s.key}`: " + ("; ".join(where) if where else "**not declared in any pom**"))
    for s in a.optional:
        where = [w for k in (s.key, *s.aliases) for w in declared(k)]
        if where:
            L.append(f"- optional `{s.key}` (configured {s.version or 'managed'}): " + "; ".join(where))
    for k in a.remove_when_present:
        where = declared(k)
        if where:
            L.append(f"- `{k}` (artifacts.remove_when_present) still declared: " + "; ".join(where))
    javax: list[str] = []
    jakarta: list[str] = []
    for p in poms:
        for d in p.dependencies:
            item = f"`{d.coordinates}`" + (f" ({d.scope})" if d.scope else "") + f" in `{p.path}`"
            if _is_javax_api(d):
                javax.append(item)
            elif d.group_id.startswith("jakarta."):
                jakarta.append(item)
    if javax:
        L.append("- javax.* API dependencies declared: " + "; ".join(javax[:30])
                 + (f" (+{len(javax) - 30})" if len(javax) > 30 else ""))
    if jakarta:
        L.append("- jakarta.* API dependencies declared: " + "; ".join(jakarta[:30])
                 + (f" (+{len(jakarta) - 30})" if len(jakarta) > 30 else ""))
    for p in poms:
        for title, deps in (("dependencies", p.dependencies), ("dependencyManagement", p.dependency_management)):
            seen: dict[tuple, list[GAV]] = {}
            for d in deps:
                seen.setdefault((d.key, d.classifier or "", d.type or "jar"), []).append(d)
            for (key, _c, _t), ds in seen.items():
                if len(ds) > 1:
                    vs = ", ".join(f"{d.version or 'managed'}" + (f"/{d.scope}" if d.scope else "") for d in ds)
                    L.append(f"- `{p.path}` declares `{key}` {len(ds)} times in {title}: {vs}")
    return L


MAX_BINARY_FACTS = 12


def filtered_binary_lines(ws: Any, poms: list[PomInfo]) -> list[str]:
    """`<filtering>true</filtering>` 리소스 아래의 바이너리 파일: 빌드는 통과하지만 Maven 필터가 패키지 안의
    바이트를 바꿔 버린다(v1 은 선처리 핸들러 E12 로 고쳤고, v2 는 구조 검토에 맡긴다). 예외를 내지 않는다.
    v2.1m: 재구성이 ``<nonFilteredFileExtensions>`` 를 이미 적으므로 아직 덮이지 않은 것(보통 없음 — 확장자 없는 파일
    정도)만 보인다.

    Binary files a `<filtering>true</filtering>` resource runs through Maven's filter (the analyzer's scan,
    `sbmig.analyze.properties.filtered_binary_resources`, on the restructured tree): the build passes, but the packaged
    bytes are rewritten — v1 fixed this with a pre-emptive handler (E12), v2 leaves it to the review (2026-10-26).
    v2.1m: the restructure writes `<nonFilteredFileExtensions>` itself, so only what is left is listed
    (`resources_filtering.binary_resource_plan(...).files`: the files no plugin entry covers yet, normally none).
    Never raises."""
    try:
        from sbmig.restructure.resources_filtering import binary_resource_plan
        files = binary_resource_plan(ws, None, poms).files
    except Exception:  # a fact line is never worth a failed review
        log.debug("filtered binary resource scan for the review failed", exc_info=True)
        return []
    if not files:
        return []
    exts = sorted({f.rsplit(".", 1)[1].lower() for f in files if "." in posixpath.basename(f)})
    shown = ", ".join(f"`{f}`" for f in files[:MAX_BINARY_FACTS])
    more = f" (+{len(files) - MAX_BINARY_FACTS})" if len(files) > MAX_BINARY_FACTS else ""
    return [f"- binary files under a filtered `<resource>` (Maven filtering rewrites their bytes unless "
            f"maven-resources-plugin `<nonFilteredFileExtensions>` lists the extension): {shown}{more}"
            + (f"; extensions: {', '.join(exts)}" if exts else "")]


def select_poms(ws: Any, poms: list[PomInfo], max_poms: int, max_chars: int) -> tuple[list[PomText], list[str]]:
    """보여 줄 pom 고르기: 모듈 순서대로 `max_poms` 개까지(나머지는
    이름만), 글자 예산은 allot 으로 나누고 cut_text 로 자른다."""
    from sbmig.analyze.pom_parser import module_order

    order = module_order(poms) if poms else []
    order += [p.path for p in poms if p.path not in order]
    shown, omitted = order[: max(0, max_poms)], order[max(0, max_poms):]
    texts: list[str] = []
    for path in shown:
        try:
            texts.append(ws.read(path))
        except Exception as e:  # pragma: no cover - unreadable file
            texts.append(f"<!-- sbmig: could not read this pom: {e} -->")
    caps = allot([len(t) for t in texts], max_chars)
    out: list[PomText] = []
    for path, text, cap in zip(shown, texts, caps):
        shown_text, cut = cut_text(text, cap)
        out.append(PomText(path=path, text=shown_text, chars=len(text), cut=cut))
    return out, omitted


def collect_review_input(ws: Any, profile: Optional[ProjectProfile], cfg: Any, *, notes: Iterable[str] = (),
                         results: Iterable[Any] = ()) -> ReviewInput:
    """재구성 뒤 워크스페이스에서 검토 프롬프트에 들어갈 것을 전부 읽어 ReviewInput 으로 만든다(읽기만 한다).

    Everything the review prompt shows, read from the workspace after the restructure (see module docstring)."""
    from sbmig.analyze.pom_parser import parse_poms

    poms, warnings = parse_poms(ws)
    name = _project_name(cfg, poms, profile)
    inp = ReviewInput(pom_infos=poms, warnings=list(warnings),
                      language=str(getattr(getattr(cfg, "report", None), "language", "en") or "en").lower())
    inp.project = project_lines(profile, cfg, name)
    inp.rules, inp.external_parent = rule_lines(cfg, profile, name)
    inp.restructure = restructure_lines(notes, results)
    inp.modules = module_lines(poms, ws)
    inp.facts = fact_lines(poms, cfg) + filtered_binary_lines(ws, poms) \
        + [f"- pom parse warning: {_cap(w)}" for w in warnings[:10]]
    inp.encodings = source_encodings(ws, poms, getattr(cfg.build, "encoding_candidates", None),
                                     skip_tests=bool(getattr(cfg.build, "skip_test_compile", True)))
    inp.poms, inp.omitted_poms = select_poms(ws, poms, int(cfg.review.max_poms), int(cfg.review.max_pom_chars))
    return inp
