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


def _is_import(s: Any) -> bool:
    # 재구성의 `_mandatory_managed` 와 같은 규칙: scope import 만 <dependencyManagement> 에 둔다
    return str(getattr(s, "scope", None) or "").strip().lower() == "import"


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
    ask_script: bool = False                    # v2.1y: review.ask_script (the last line asks for <script> too)
    # v2.1y: what the reviewer's checks after the answer need (unsupported issues, invariants) — not rendered
    chain: Any = None                           # ParentChainFacts (None = not readable)
    raw: dict[str, Any] = field(default_factory=dict)        # pom path → RawPom before the review's edits
    results: list[Any] = field(default_factory=list)         # the RestructureResults

    @property
    def pom_chars(self) -> int:
        """프롬프트에 실린 pom 글자 수 합."""
        return sum(len(p.text) for p in self.poms)

    @property
    def encoding_mismatches(self) -> list[ModuleEncoding]:
        """인코딩 불일치가 있는 모듈들(LLM 이 놓치면 검토기가 critical 로 더한다)."""
        return [m for m in self.encodings if m.mismatch]

    @property
    def complete(self) -> bool:
        """모든 pom 이 잘리지 않고 다 실렸는가(아니면 '재료에 없음' 판정을 pom 본문에 기대지 않는다)."""
        return not self.omitted_poms and not any(p.cut for p in self.poms)

    def full_poms(self) -> str:
        """잘리지 않은 모든 pom 본문(프롬프트에서 잘렸거나 빠진 부분 포함) — '재료에 없음' 판정이 예산 때문에 틀리지 않게."""
        return "\n".join(getattr(rp, "text", "") or "" for rp in (self.raw or {}).values())

    def material_text(self) -> str:
        """v2.1y (E-M5): 이슈를 대조할 재료 — 프롬프트 전체 + 잘리지 않은 pom 본문.

        The material an issue is checked against: the whole prompt + every pom in full (cut / omitted parts too)."""
        return self.render() + "\n" + self.full_poms()

    def state_text(self) -> str:
        """v2.1y (E-M5): 지금 상태를 보여 주는 재료 — "What the restructure did"(지운 것·바꾼 것의 기록)를 뺀 프롬프트 + 잘리지
        않은 pom 본문. 속성 이름이 여기 없으면 그 속성은 어느 pom 에도 없다.

        The prompt without "What the restructure did" (a record of what was removed / changed) + every pom in full: a
        property named nowhere here is in no pom."""
        saved, self.restructure = self.restructure, []
        try:
            return self.render() + "\n" + self.full_poms()
        finally:
            self.restructure = saved

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
        # v2.1y (D-M1): 마지막 줄이 시스템 프롬프트의 답 형식과 같아야 한다(예전엔 `<script>` 만 요구해 edits 가 빠졌다)
        ask = ("`<analysis>`, `<solution>` and `<edits>`" + (", optionally `<script>` with `<changes>`"
                                                               if self.ask_script else ""))
        L += ["# Your task", "",
              "Review the build structure above against the company rules and answer in the format of the system "
              f"prompt: one `<review>` JSON block and — only for critical issues you can fix in the build files — {ask}."]
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


def _first_effective(results: Iterable[Any]) -> Optional[bool]:
    for r in results or ():
        v = _field(r, "parent_manages_spring_boot_effective") if r is not None else None
        if v is not None:
            return bool(v)
    return None


_BOM_IMPORT_RE = re.compile(r"^(?P<pom>.+?): (?P<ga>[^\s:]+:[^\s:]+) (?P<v>\S+)(?: \((?P<rest>.*)\))?$")


def bom_import_groups(results: Iterable[Any]) -> list[tuple[str, str, str, list[str]]]:
    """재구성 결과의 `bom_imports` 줄("<pom>: g:a <값> (property of <pom> | no property …)")을 (g:a, 값, 괄호 안, pom
    목록)으로 묶는다(같은 import 를 여러 pom 에 적었으면 한 묶음).

    `RestructureResult.bom_imports` lines grouped as (g:a, value, remark, poms)."""
    groups: dict[tuple[str, str, str], list[str]] = {}
    for r in results or ():
        for line in (_field(r, "bom_imports") or []) if r is not None else []:
            m = _BOM_IMPORT_RE.match(str(line).strip())
            if not m:
                continue
            key = (m.group("ga"), m.group("v"), m.group("rest") or "")
            if m.group("pom") not in groups.setdefault(key, []):
                groups[key].append(m.group("pom"))
    return [(ga, v, rest, poms) for (ga, v, rest), poms in groups.items()]


def boot_version_lines(cfg: Any, results: Iterable[Any], facts: Any = None) -> list[str]:
    """v2.1v (D-H2): Spring Boot 버전 규칙 줄 — 설정 문구가 아니라 재구성이 정한 실효값(`parent_manages_spring_boot_effective`)과
    재구성이 적은 BOM import 로 쓴다. 목표 버전 리터럴은 규칙으로 쓰지 않는다(Project 절의 target 만).

    The "Spring Boot versions" rule from the restructure's effective decision and its BOM imports — never the target
    literal as a rule (the target appears in the Project section only)."""
    a = cfg.artifacts
    results = [r for r in (results or ()) if r is not None]
    eff = _first_effective(results)
    props = dict(getattr(facts, "properties", None) or {}) if facts is not None else {}
    imports = bom_import_groups(results)
    L: list[str] = []
    for ga, v, rest, poms in imports:
        where = ", ".join(f"`{p}`" for p in poms[:8]) + (f" (+{len(poms) - 8})" if len(poms) > 8 else "")
        m = _PROP_REF.match(v)
        if m:
            prop = m.group(1)
            value = props.get(prop)
            src = rest[len("property of "):] if rest.startswith("property of ") else "the parent chain"
            L.append(f"- BOM import by the restructure: `{ga}` `{v}`" + (f" (= {value}, defined by {src})" if value
                                                                     else f" (defined by {src})")
                     + f" in {where} — the property resolves through Maven inheritance: never define `{prop}` in a "
                       "project pom")
        else:
            L.append(f"- BOM import by the restructure: `{ga}` {v} in {where} (the parent chain defines no property "
                     "for it)")
    boot_import = any(ga.endswith(":spring-boot-dependencies") for ga, _v, _r, _p in imports)
    if eff is True and not boot_import:
        boot_v = ("the parent chain reaches the Boot BOM: no Boot BOM import and no Boot version property belongs in "
                  "the project poms")
    elif boot_import:
        boot_v = "the parent chain does not reach the Boot BOM: the restructure imported it (BOM import above)"
    elif eff is False:
        boot_v = ("the parent chain does not reach the Boot BOM and the restructure recorded no Boot BOM import — check "
                  "the poms' `spring-boot-dependencies` import")
    elif a.parent_manages_spring_boot == "auto":
        boot_v = "decided by the restructure from the parent chain's poms (see \"What the restructure did\")"
    else:
        boot_v = ("managed by the parent chain" if a.parent_manages_spring_boot
                  else "a `spring-boot-dependencies` import in the poms (see the restructure's BOM imports)")
    java_v = ("managed by the parent chain" if a.parent_manages_java_version
              else f"maven.compiler.release {cfg.target.java_version} in the poms")
    return [f"- Spring Boot versions: {boot_v}; Java level: {java_v}"] + L


def _starter(cfg: Any, case: CaseType, results: Iterable[Any]) -> str:
    for r in results or ():
        v = _field(r, "starter") if r is not None else None
        if v:
            return str(v)
    v = (getattr(cfg.layout, "starter_by_case", None) or {}).get(case.value)
    return "" if not v or v == "auto" else str(v)


def _msa_line(cfg: Any, profile: Optional[ProjectProfile]) -> str:
    """v2.1v (D-H3): MSA 규칙 — 코드(`restructurer._msa`)가 하는 그대로: 서비스 루트 → boot 부모(역할 부모가 없으면 루트
    부모) + 필수, 라이브러리 루트 → comp 부모(또는 루트 부모), 필수 없음, 최상위 aggregator → 루트 부모(+ 필수). 이름은
    `profile.services` 에서."""
    a, lay = cfg.artifacts, cfg.layout
    try:
        from sbmig.analyze.classifier import is_library_root
    except Exception:  # noqa: BLE001 - the analyzer is always there; a fact line is never worth a failed review
        def is_library_root(svc: Any, prof: Any = None) -> bool:
            return getattr(svc, "kind", "service") == "library"
    services = list(getattr(profile, "services", None) or []) if profile is not None else []
    libs = [x for x in services if is_library_root(x, profile)]
    svcs = [x for x in services if x not in libs]

    def names(xs: list[Any]) -> str:
        shown = ", ".join(f"`{x.name}`" for x in xs[:12]) + (f" (+{len(xs) - 12})" if len(xs) > 12 else "")
        return shown or "none"
    head = "- layout (MSA): directories untouched, poms only; module names are not checked; a `war` service keeps its packaging. "
    if lay.msa_parent_policy == "top-aggregator":
        return (head + f"`layout.msa_parent_policy: top-aggregator`: only the top aggregator gets the root parent and "
                f"the mandatory dependencies; service roots ({names(svcs)}) and library roots ({names(libs)}) inherit "
                "them")
    role = bool(a.role_parents)
    svc_parent = "the boot parent" if role else "the root parent"
    lib_parent = "the comp parent" if role else "the root parent"
    agg = ("the top aggregator → the root parent" + (" + the mandatory dependencies" if a.mandatory_target == "root"
                                                     else "")
           if role else "the top aggregator keeps its own parent (not changed)")
    return (head + f"Service roots ({names(svcs)}) → {svc_parent} + the mandatory dependencies; library roots "
            f"({names(libs)}) → {lib_parent}, no mandatory dependencies; {agg}")


def rule_lines(cfg: Any, profile: Optional[ProjectProfile], name: str, results: Iterable[Any] = (),
               facts: Any = None) -> tuple[list[str], str]:
    """이 케이스에 맞는 회사 규칙 줄들(`cfg.artifacts` / `cfg.layout` 에서 읽음)과 외부 부모를 부르는 이름.
    레이아웃은 케이스별: MSA(디렉토리 그대로, pom 만), 분리 대상(spring_legacy / servlet_tomcat / pure_java →
    `{name}-boot` / `-back` / `-front`), Boot as-is(pom 만, 단일 모듈은 `{name}-boot` 로 감쌈). v2.1v: Spring Boot 버전 줄은
    재구성의 실효값·BOM import 로(`results`, `facts` = 부모 체인 사실), 필수 줄은 스펙마다 `→ <dependencies>` /
    `→ <dependencyManagement>`, 선택 줄은 v2.1n / v2.1t 규칙, 패키징은 실행 가능한 WAR, boot 모듈의 starter.

    The company rules from `cfg.artifacts` / `cfg.layout` for this case, and how to name the external parent."""
    a, lay = cfg.artifacts, cfg.layout
    case = profile.case_type if profile is not None else CaseType.UNKNOWN
    results = [r for r in (results or ()) if r is not None]
    L = [f"- root pom parent (`artifacts.parent`): {_spec(a.parent)}"]
    if a.role_parents:
        L.append(f"- boot module parent (`artifacts.boot_parent`): {_spec(a.boot_parent)}")
        L.append(f"- every other module's parent (`artifacts.comp_parent`): {_spec(a.comp_parent)}; the root pom is "
                 "then an aggregator the modules do not inherit from")
        external = f"the role parents `{a.boot_parent.key}` / `{a.comp_parent.key}`"
    else:
        L.append("- no role parents configured: " + ("every service / library root gets the root parent"
                                                      if case == CaseType.MSA else "the modules inherit the root pom"))
        external = f"the parent `{a.parent.key}`"
    if a.mandatory:
        where = ("the service roots (see the layout line)" if case == CaseType.MSA else
                 "the root pom" if a.mandatory_target == "root" else "every module pom (boot / comp role modules)")
        L.append(f"- mandatory dependencies, declared in {where} (`artifacts.mandatory_target: {a.mandatory_target}`)"
                 + ("; with role parents a module does not inherit the root pom's declarations (company rule — never "
                    "copy them into the modules)" if a.role_parents and a.mandatory_target == "root" else "") + ":")
        L += [f"  - {_spec(s)} → " + ("`<dependencyManagement>` (BOM import)" if _is_import(s) else "`<dependencies>`")
              for s in a.mandatory]
    if a.optional:
        L.append("- optional in-house artifacts — declared only where already used, or added where one of their trigger "
                 "dependencies (`when_present`) is declared (`replaces` removes the trigger); version "
                 "`${<artifact>.version}` when the parent chain defines that property, else the configured one — a "
                 f"newer as-is version is kept (`optional_version_policy: {a.optional_version_policy}`):")
        for s in a.optional:
            extra = []
            if s.aliases:
                extra.append(f"aliases {', '.join(s.aliases)}")
            if getattr(s, "when_present", None):
                extra.append(f"added for {', '.join(s.when_present)}" + ("; replaces them" if s.replaces
                                                                          else "; next to them"))
            if getattr(s, "property_name", None):
                extra.append(f"property `{s.property_name}`")
            L.append(f"  - {_spec(s)}" + (f" ({'; '.join(extra)})" if extra else ""))
    if a.remove_when_present:
        L.append("- must not be declared any more (`artifacts.remove_when_present`): "
                 + ", ".join(f"`{k}`" for k in a.remove_when_present))
    L += boot_version_lines(cfg, results, facts)
    # v2.1v (D-M6): 재구성의 버전 결정은 최종 — 검토가 "같은 메이저로" 다시 고정하지 않게
    L.append("- the version decisions under \"What the restructure did\" (dropped / pinned / carried-over versions, "
             "removed properties, BOM imports, added in-house artifacts) are final: never re-add, re-pin or "
             "re-literalize them")
    boot = lay.module_path("boot", name)
    comp = lay.module_path("comp", name)
    front = lay.module_path("front", name)
    jsp = bool(profile is not None and profile.source.jsp_count)
    web = bool(profile is not None and (profile.source.jsp_count or profile.source.web_static_count))
    split = case in (CaseType.SPRING_LEGACY, CaseType.SERVLET_TOMCAT, CaseType.PURE_JAVA, CaseType.UNKNOWN) or (
        case == CaseType.SPRING_BOOT and lay.boot_as_is == "layout")
    if case == CaseType.MSA:
        L.append(_msa_line(cfg, profile))
    elif split:
        # v2.1q / v2.1u (D-H4): JSP 가 있으면 실행 가능한 WAR(java -jar 로 내장 Tomcat) — 외부 Tomcat 은 대안일 뿐
        pkg = ("war (executable WAR: `java -jar` runs the embedded Tomcat, the same file deploys to an external Tomcat "
               "too; `spring-boot-starter-tomcat` / `tomcat-embed-jasper` stay `provided`, packaged under "
               "WEB-INF/lib-provided)" if jsp and lay.jsp_boot_packaging == "war" else "jar (executable jar)")
        # 재구성이 실제로 만든 모듈 경로가 있으면 그것(설정의 이름 규칙보다 정확하다)
        r_boot = next((_field(r, "boot_module") for r in results if _field(r, "boot_module")), None)
        comp = next((_field(r, "comp_module") for r in results if _field(r, "comp_module")), None) or comp
        front = next((_field(r, "front_module") for r in results if _field(r, "front_module")), None) or front
        L.append(f"- layout (case `{case.value}`): the root is a `pom` aggregator; modules directly under it:")
        L.append(f"  - `{r_boot or boot}`: Boot configuration only (application class, application.* resources), "
                 f"packaging {pkg}, parent = " + ("boot parent" if a.role_parents else "root pom"))
        L.append(f"  - `{comp}`: every source incl. controllers, packaging jar, parent = "
                 + ("comp parent" if a.role_parents else "root pom"))
        if front and web:
            L.append(f"  - `{front}`: web resources only (static under `{lay.front_static_dir}`, JSPs under "
                     f"`{lay.front_jsp_dir}`), packaging jar, parent = " + ("comp parent" if a.role_parents
                                                                             else "root pom"))
        elif front:
            L.append(f"  - `{front}`: only when the project has web resources (none detected)")
        starter = _starter(cfg, case, results)
        L.append(f"  - the boot module depends on `{comp}`" + (f" and `{front}`" if front and web else "")
                 + (f" and the starter `{starter}`" if starter else "")
                 + "; do not add Security / Batch / JPA starters (their auto-configuration changes the runtime)")
        if lay.module_dir:
            L.append(f"- modules live under `{lay.module_dir}/`")
    else:
        L.append(f"- layout (Spring Boot as-is, `layout.boot_as_is: {lay.boot_as_is}`): directories are kept, only the "
                 f"poms change — root → root parent; module dirs named like `{lay.boot_module_pattern}` whose pom is a "
                 "Boot app → boot parent; every other module → comp parent; a single-module Boot app is wrapped "
                 f"into `{boot}` under an aggregator root; a `war` module keeps its packaging")
    return L, external


def _field(r: Any, key: str, default: Any = None) -> Any:
    return r.get(key, default) if isinstance(r, dict) else getattr(r, key, default)


# (field, title) of the list fields of RestructureResult rendered under "What the restructure did", in this order
_RESULT_LISTS: tuple[tuple[str, str], ...] = (
    ("parents_set", "parents set"), ("mandatory_added", "mandatory added"),
    ("optional_bumped", "optional bumped"),
    # v2.1n: 트리거 의존성 때문에 새로 넣은 선택 아티팩트와 그때 지운 트리거
    ("optional_added", "optional artifacts added for trigger dependencies"),
    ("optional_replaced", "trigger dependencies replaced by optional artifacts"),
    ("merged_modules", "merged modules"),
    ("managed_versions_dropped", "explicit versions dropped (managed by the new parents)"),
    # v2.1o: 레시피가 관리 버전보다 높여 놓은 명시 버전도 지웠다(as-is 는 관리 버전 이하)
    ("recipe_versions_dropped", "recipe-raised versions dropped (the as-is build was at or below the managed version)"),
    # v2.1l: 검토도 재구성이 적은 as-is 버전을 다른 pom 교정처럼 본다
    ("versions_pinned", "versions carried over from the as-is build"),
    ("versions_unknown", "version-less dependencies the as-is build did not resolve"),
    # v2.1o: 부모의 Boot BOM 속성을 덮어쓰던 as-is 속성을 재구성이 이미 지웠다
    ("bom_properties_removed",
     "as-is version properties removed (they overrode the parent's Spring Boot BOM properties)"),
    # v2.1t: 재구성이 적은 BOM import(부모 체인 속성 참조 또는 리터럴)
    ("bom_imports", "BOM imports written by the restructure"),
    ("version_properties_removed", "version properties removed (only the dropped explicit versions used them)"),
    # v2.1x (E-M6 / D-M7 / D2 / D1 / D6 / D9): 전환 시점에 재구성이 직접 한 일 — 검토가 되돌리지 않게
    ("scope_restored", "scopes restored from the as-is build"),
    ("source_encodings_written", "source encodings written"),
    ("non_filtered_extensions", "maven-resources-plugin nonFilteredFileExtensions written"),
    ("xml_imports", "XML bean configs imported via @ImportResource"),
    ("xml_skipped", "XML bean configs not imported"),
    ("legacy_properties_written", "legacy context properties written (boot application.properties)"),
    ("webinf_locations_rewritten", "/WEB-INF/ locations rewritten in the legacy-config copies"),
    ("beans_removed", "beans removed from the legacy-config copies"),
    ("duplicate_bean_ids", "duplicate bean ids across the imported XML"),
    ("generated_files", "generated files"), ("flags", "flags"),
    ("manual_followups", "follow-ups"),
)
_STEP_PREFIX_RE = re.compile(r"^[\w.-]+(?: \([\w-]+\))?: ")
_COVER_MIN = 16                    # a structured item at least this long …
_COVER_SHARE = 0.5                 # … that makes up at least half of a note makes the note redundant


def _norm(text: object) -> str:
    return " ".join(str(text if text is not None else "").split())


def _covered(note: str, items: list[str]) -> bool:
    # 메모가 구조화된 항목 하나를 거의 그대로 되풀이할 때만 뺀다 — 더 많은 것을 말하는 메모는 남긴다
    n = _norm(note)
    return any(n == i or (len(i) >= _COVER_MIN and len(i) >= _COVER_SHARE * len(n) and i in n) for i in items)


def _list_block(out: list[str], title: str, items: list[str], indent: str = "  ") -> None:
    out.append(f"{indent}- {title}:")
    out += [f"{indent}  - {_cap(x)}" for x in items[:SECTION_LINES_MAX]]
    if len(items) > SECTION_LINES_MAX:
        out.append(f"{indent}  - … {len(items) - SECTION_LINES_MAX} more")


def restructure_lines(notes: Iterable[str], results: Iterable[Any]) -> list[str]:
    """재구성이 한 일: RestructureResult(또는 그 필드의 dict)마다 한 블록 + 실행 메모. 목록은 SECTION_LINES_MAX 줄까지.
    v2.1v (D-M3 / E-L1): v2.1x 의 새 필드(starter, web_evidence, xml_imports, … conversion_facts 는 `- key: value`)와
    `parent_manages_spring_boot_effective`, `non_filtered_extensions`, `jsp_jakarta_*` 도 보이고, 결과의 notes·실행 메모는
    이미 구조화된 항목(또는 앞의 메모)이 같은 내용을 말하면 다시 싣지 않는다(14/14 실행에서 1.1K 자가 반복되던 것).

    What the restructure did: one block per `RestructureResult` (or dict of its fields) + the run notes that no result
    already covers (de-duplicated by text)."""
    out: list[str] = []
    seen: list[str] = []                         # every structured item / note rendered (normalized), for de-duplication
    for r in results or ():
        if r is None:
            continue

        def g(key: str, default: Any = None, _r: Any = r) -> Any:
            return _field(_r, key, default)
        state = "ok" if g("ok", True) else f"FAILED: {_cap(g('error') or '?')}"
        layout = ", ".join(f"{k} `{g(k)}`" for k in ("boot_module", "comp_module", "front_module", "boot_packaging",
                                                      "kept_boot_module") if g(k))
        out.append(f"- restructure result: {state}" + (f"; {layout}" if layout else ""))
        if g("starter"):
            out.append(f"  - boot starter: `{g('starter')}`")
        if g("web_evidence"):
            out.append(f"  - web evidence: {_cap(', '.join(str(x) for x in g('web_evidence')))}")
        eff = g("parent_manages_spring_boot_effective")
        if eff is not None:
            out.append(f"  - parent chain manages Spring Boot (effective): {'yes' if eff else 'no'}")
        if int(g("jsp_jakarta_files", 0) or 0):
            out.append(f"  - JSP / tag files with javax → jakarta names renamed: {int(g('jsp_jakarta_files') or 0)} "
                       f"file(s), {int(g('jsp_jakarta_refs', 0) or 0)} reference(s)")
        for key, title in _RESULT_LISTS:
            items = [x for x in (g(key) or []) if x]
            if not items:
                continue
            _list_block(out, title, items)
            seen += [_norm(x) for x in items]
        facts = g("conversion_facts") or {}
        if isinstance(facts, dict) and facts:
            out.append("  - conversion facts:")
            for k, v in list(facts.items())[:SECTION_LINES_MAX]:
                val = ", ".join(str(x) for x in v) if isinstance(v, (list, tuple, set)) else str(v)
                out.append(f"    - {k}: {_cap(val)}")
                seen.append(_norm(val))
        kept = []
        for n in (g("notes") or []):
            if n and not _covered(n, seen) and _norm(n) not in kept:
                kept.append(_norm(n))
        if kept:
            _list_block(out, "notes", kept)
        seen += kept
        conflicts = g("merge_conflicts") or []
        if conflicts:
            out.append(f"  - merge conflicts (first-wins): {len(conflicts)}")
    extra: list[str] = []
    for n in (notes or ()):
        if not n:
            continue
        body = _STEP_PREFIX_RE.sub("", str(n), count=1)
        if _covered(body, seen) or _covered(n, seen) or _norm(n) in extra:
            continue
        extra.append(_norm(n))
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


# ---------------------------------------------------------------------------- version facts (v2.1v)
BOOT_GROUP = "org.springframework.boot"
BOOT_BOM_KEY = f"{BOOT_GROUP}:spring-boot-dependencies"
MAX_CHAIN_PROPERTY_LINES = 20
MAX_VERSION_FACTS = 30
_PROP_ANY = re.compile(r"\$\{([^}]+)\}")
# 서블릿 / JSP / EL / websocket API — 컨테이너(내장 Tomcat)가 주므로 provided 가 맞다(D-M9)
_CONTAINER_APIS = {"servlet-api", "javax.servlet-api", "jakarta.servlet-api", "jsp-api", "javax.servlet.jsp-api",
                   "jakarta.servlet.jsp-api", "el-api", "javax.el-api", "jakarta.el-api", "websocket-api",
                   "javax.websocket-api", "jakarta.websocket-api", "jakarta.websocket-client-api"}


def chain_facts(cfg: Any) -> Any:
    """사내 부모 체인의 사실(`sbmig.build.parent_chain.load_parent_chain_facts`, 프로세스 캐시) — 못 읽으면 None. 예외 없음.

    The parent-chain facts of the configured company parents (module attribute call, so the tests' stand-in applies);
    None when nothing could be read. Never raises."""
    try:
        from sbmig.build import parent_chain
        facts = parent_chain.load_parent_chain_facts(cfg)
    except Exception:  # facts are an aid
        log.debug("parent chain facts for the review unavailable", exc_info=True)
        return None
    return None if facts is None or getattr(facts, "empty", True) else facts


def is_version_property(name: str) -> bool:
    """버전 속성 이름인가(`*.version`, `java.version` 은 아님) — 재구성의 덮어쓰기 이름 규칙(v2.1p)과 같다. 인코딩 같은
    다른 회사 속성(`project.build.sourceEncoding`)은 모듈 pom 에 써도 되므로 이 검사에 넣지 않는다.

    `*.version` but never `java.version` (the restructure's override-name rule); other company properties such as
    `project.build.sourceEncoding` may be set in a module pom."""
    n = (name or "").strip()
    return n.endswith(".version") and n != "java.version" and not n.startswith(("project.", "maven."))


def company_properties(facts: Any, cfg: Any) -> tuple[dict[str, str], dict[str, str]]:
    """회사 체인 pom 이 정의한 속성(이름 → 값)과 정의한 pom — 로더의 `company_properties` 가 있으면 그것, 없으면 같은
    규칙(루트·필수·선택 아티팩트의 group, `org.springframework*` 제외)으로 `properties` 에서 고른다.

    The properties the COMPANY's own chain poms define and their defining pom (the loader's split, else the same rule)."""
    if facts is None:
        return {}, {}
    own = dict(getattr(facts, "company_properties", None) or {})
    if own:
        return own, dict(getattr(facts, "company_property_sources", None) or {})
    try:
        from sbmig.build.parent_chain import is_company_group
    except Exception:  # noqa: BLE001
        return {}, {}
    a = getattr(cfg, "artifacts", None)
    groups = [str(r).split(":")[0] for r in getattr(facts, "roots", None) or [] if r]
    groups += [str(getattr(s, "group_id", "") or "") for s in [*(getattr(a, "mandatory", None) or []),
                                                               *(getattr(a, "optional", None) or [])]]
    groups = [g for g in dict.fromkeys(groups) if g]
    props, srcs = dict(getattr(facts, "properties", None) or {}), dict(getattr(facts, "property_sources", None) or {})
    keep = {k: v for k, v in props.items() if is_company_group(srcs.get(k, "").split(":")[0], groups)}
    return keep, {k: srcs.get(k, "") for k in keep}


def _newer(a: str, b: str) -> Optional[bool]:
    """`a` 가 `b` 보다 새 버전인가(못 읽으면 None)."""
    try:
        from sbmig.plan.versions import compare_versions
        c = compare_versions(a, b)
    except Exception:  # an unreadable version is no decision
        return None
    return None if c is None else c > 0


def _major_minor(v: Optional[str]) -> Optional[tuple[str, str]]:
    m = re.match(r"\s*(\d+)\.(\d+)", v or "")
    return (m.group(1), m.group(2)) if m else None


@dataclass
class RawPom:
    """검토가 쓰는 pom 하나의 원시 값(치환 전 `<version>` / `<scope>` 그대로 — PomInfo 는 저장소 안 관리 버전으로 채운다)."""
    path: str
    raw: Any                       # sbmig.analyze.pom_parser._RawPom
    text: str = ""


def raw_poms(ws: Any, poms: list[PomInfo]) -> dict[str, RawPom]:
    """프로젝트 pom 들의 원시 파싱(읽지 못하거나 깨진 pom 은 빠진다). 예외 없음."""
    from sbmig.analyze.pom_parser import parse_pom_text

    out: dict[str, RawPom] = {}
    for p in poms:
        try:
            text = ws.read(p.path)
            out[p.path] = RawPom(path=p.path, raw=parse_pom_text(text, p.path), text=text)
        except Exception:  # noqa: BLE001 - the parse warnings already name it
            continue
    return out


def _resolver(pom: PomInfo, poms: list[PomInfo], base: dict[str, str]):
    """`${x}` 풀이: 그 pom 의 프로젝트 안 부모 사슬 속성(자식이 이김) → 회사 체인 속성(`base`)."""
    props = dict(base)
    for q in reversed(_chain(pom, poms)):
        props.update(q.properties or {})
    builtins = {"project.version": pom.version or "", "project.groupId": pom.group_id or ""}

    def resolve(v: Optional[str]) -> str:
        s = (v or "").strip()
        for _ in range(4):                       # nested ${…} a few levels deep; an unknown one stays as it is
            nxt = _PROP_ANY.sub(lambda m: props.get(m.group(1)) or builtins.get(m.group(1)) or m.group(0), s)
            if nxt == s:
                break
            s = nxt
        return s
    return resolve


def _skip_keys(cfg: Any, poms: list[PomInfo], results: Iterable[Any]) -> set[str]:
    """명시 버전 사실에서 뺄 g:a: 프로젝트 자신의 모듈, 필수·선택 사내 아티팩트(설정 버전이 규칙), 재구성이 as-is 에서 옮겨
    적은 버전(최종 결정, D-M6)."""
    a = cfg.artifacts
    keys = {f"{p.group_id}:{p.artifact_id}" for p in poms if p.group_id}
    for s in [*(a.mandatory or []), *(a.optional or []), *a.parent_specs()]:
        keys.add(s.key)
        keys.update(getattr(s, "aliases", None) or [])
    for r in results or ():
        for line in (_field(r, "versions_pinned") or []) if r is not None else []:
            keys.add(str(line).split(" ", 1)[0].rstrip(":"))
    return keys


def managed_versions(cfg: Any, results: Iterable[Any], facts: Any) -> tuple[dict[str, str], dict[str, str]]:
    """(g:a → 관리 버전, g:a → 누가 관리하는지): 사내 체인 자신의 관리 목록(`own_managed`, Boot 보탬 제외) + 재구성이
    Boot BOM 을 import 했으면 그 BOM(`sbmig.build.boot_bom.load_boot_bom_facts`, 읽기 전용). 예외 없음.

    The managed versions a project pom inherits: the chain's own entries (without the Boot add-on) + the Boot BOM the
    restructure imported (when it did). Never raises."""
    managed: dict[str, str] = {}
    by: dict[str, str] = {}
    if facts is None:
        return managed, by
    roots = list(getattr(facts, "roots", None) or [])
    chain_label = f"the parent chain (`{roots[0]}`)" if roots else "the parent chain"
    try:
        own = getattr(facts, "own_managed", None)
        own = own if isinstance(own, dict) else dict(getattr(facts, "managed", None) or {})
    except Exception:  # noqa: BLE001
        own = {}
    for k, v in own.items():
        managed[k], by[k] = v, chain_label
    boot = [(ga, v) for ga, v, _r, _p in bom_import_groups(results) if ga == BOOT_BOM_KEY]
    if boot:
        ga, v = boot[0]
        m = _PROP_REF.match(v)
        version = (getattr(facts, "properties", None) or {}).get(m.group(1)) if m else v
        try:
            from sbmig.build import boot_bom
            bb = boot_bom.load_boot_bom_facts(cfg, version or None)
            for k, mv in (getattr(bb, "managed", None) or {}).items():
                if k not in managed:
                    managed[k], by[k] = mv, f"the imported Boot BOM (`{ga}:{version or v}`)"
        except Exception:  # facts are an aid
            log.debug("Boot BOM facts for the review unavailable", exc_info=True)
    return managed, by


def version_fact_lines(poms: list[PomInfo], cfg: Any, results: Iterable[Any] = (), *, facts: Any = None,
                       raw: Optional[dict[str, RawPom]] = None, ws: Any = None) -> list[str]:
    """v2.1v (D-H1): 부모 체인의 pom 에서 읽은 버전 사실 — 프롬프트에 리터럴 없이 사실의 값만:
    `parent chain property: <name>=<value> (<정의 pom>)` (회사 체인의 `*.version`, ≤ 20),
    `explicit version on a managed artifact: <pom> g:a <v> — managed <m> by <누구>`,
    `Spring Boot artifact on another line: <pom> g:a <v> (chain <m> | target <t>)` (major.minor 가 다름),
    `property shadows the parent chain: <pom> <name>=<value> (chain: <value> by <pom>)`. 뒤의 둘은 시스템 프롬프트에서
    critical. 보통은 재구성이 다 정리해서 뒤 세 종류는 0 줄. 사실을 못 읽으면(저장소 없음) 0 줄. 예외 없음.

    The version facts read from the parent chain's poms (see the Korean summary): the company chain's `*.version`
    properties, explicit versions on managed artifacts, Spring Boot artifacts / plugins / imports on another line than
    the managed (or target) one, and project properties that shadow a company chain property. Never raises."""
    try:
        return _version_fact_lines(poms, cfg, list(results or ()), facts if facts is not None else chain_facts(cfg),
                                   raw, ws)
    except Exception:  # a fact line is never worth a failed review
        log.debug("version facts for the review failed", exc_info=True)
        return []


def _version_fact_lines(poms: list[PomInfo], cfg: Any, results: list[Any], facts: Any,
                        raw: Optional[dict[str, RawPom]], ws: Any) -> list[str]:
    if facts is None:
        return []
    if raw is None:
        raw = raw_poms(ws, poms) if ws is not None else {}
    cprops, csrc = company_properties(facts, cfg)
    vprops = {k: v for k, v in cprops.items() if is_version_property(k)}
    L = [f"- parent chain property: {k}={v} ({csrc.get(k) or 'the parent chain'})"
         for k, v in sorted(vprops.items())[:MAX_CHAIN_PROPERTY_LINES]]
    if len(vprops) > MAX_CHAIN_PROPERTY_LINES:
        L.append(f"- parent chain property: … {len(vprops) - MAX_CHAIN_PROPERTY_LINES} more")
    managed, by = managed_versions(cfg, results, facts)
    skip = _skip_keys(cfg, poms, results)
    target = str(getattr(cfg.target, "spring_boot_version", "") or "")
    parents = {s.key for s in cfg.artifacts.parent_specs()}
    base = dict(getattr(facts, "properties", None) or {})
    problems: list[str] = []
    for p in poms:
        rp = raw.get(p.path)
        if rp is None:
            continue
        resolve = _resolver(p, poms, base)
        entries = [(d, "") for d in rp.raw.deps] + [(d, " (dependencyManagement)") for d in rp.raw.managed]
        for d, where in entries:
            key = f"{resolve(d.group_id) or d.group_id}:{d.artifact_id}"
            if not d.version:
                continue
            ref = _PROP_REF.match(d.version.strip())
            if ref and ref.group(1) in cprops:
                continue                                  # ${<company property>} — the v2.1t form, decided by the chain
            v = resolve(d.version)
            if "${" in v:
                continue
            scope = (d.scope or "").lower()
            if key.startswith(BOOT_GROUP + ":") and key not in parents:
                m = managed.get(key)
                ref_v, label = (m, f"chain {m}") if m else (target, f"target {target}")
                if _major_minor(v) and _major_minor(ref_v) and _major_minor(v) != _major_minor(ref_v):
                    problems.append(f"- Spring Boot artifact on another line: `{p.path}` {key} {v}{where} ({label})")
                    continue
            if scope == "import" or key in skip:
                continue
            # 관리 버전보다 새 명시 버전은 재구성이 일부러 남긴 것(drop_managed_versions: gte, as-is 가 더 새것) — 최종 결정
            if key in managed and (_newer(v, managed[key]) is not True):
                problems.append(f"- explicit version on a managed artifact: `{p.path}` {key} {v}{where} — managed "
                                f"{managed[key]} by {by.get(key, 'the parent chain')}")
        for pl in rp.raw.plugins:
            if pl.group_id != BOOT_GROUP or not pl.version:
                continue
            v = resolve(pl.version)
            ref = _PROP_REF.match(pl.version.strip())
            if "${" in v or (ref and ref.group(1) in cprops):
                continue
            if _major_minor(v) and _major_minor(target) and _major_minor(v) != _major_minor(target):
                problems.append(f"- Spring Boot artifact on another line: `{p.path}` plugin {pl.group_id}:"
                                f"{pl.artifact_id} {v} (target {target})")
        par = rp.raw.parent
        if par is not None and par.group_id == BOOT_GROUP and par.key not in parents and par.version:
            v = resolve(par.version)
            if _major_minor(v) and _major_minor(target) and _major_minor(v) != _major_minor(target):
                problems.append(f"- Spring Boot artifact on another line: `{p.path}` parent {par.key} {v} "
                                f"(target {target})")
        for name, value in (rp.raw.properties or {}).items():
            if name in vprops:
                problems.append(f"- property shadows the parent chain: `{p.path}` {name}={value} (chain: "
                                f"{vprops[name]} by {csrc.get(name) or 'the parent chain'})")
    if len(problems) > MAX_VERSION_FACTS:
        problems = problems[:MAX_VERSION_FACTS] + [f"- … {len(problems) - MAX_VERSION_FACTS} more version facts"]
    return L + problems


def container_scope_lines(raw: dict[str, RawPom]) -> list[str]:
    """v2.1v (D-M9): `compile`(또는 scope 없음)로 선언된 컨테이너 API(servlet / JSP / EL / websocket) — warning 사실.
    재구성이 as-is 의 provided 를 되돌리므로(E-M6) 보통 0 줄.

    Container APIs declared `compile` (explicitly or by default) — a warning fact; normally none (the restructure
    restores the as-is `provided`)."""
    found: list[str] = []
    for path, rp in raw.items():
        for d in rp.raw.deps:
            if d.artifact_id in _CONTAINER_APIS and (d.scope or "compile").lower() == "compile":
                found.append(f"`{d.group_id}:{d.artifact_id}` in `{path}`"
                             + (" (scope compile)" if d.scope else " (no scope = compile)"))
    if not found:
        return []
    return ["- container API declared `compile` (the embedded Tomcat provides it; `provided` is right): "
            + "; ".join(found[:12]) + (f" (+{len(found) - 12})" if len(found) > 12 else "")]


def _boot_plugin_blocks(text: str) -> list[str]:
    """pom 텍스트의 `<build><plugins>` 안 spring-boot-maven-plugin `<plugin>` 블록들(pluginManagement 제외)."""
    body = re.sub(r"<!--.*?-->", "", text or "", flags=re.S)
    body = re.sub(r"<pluginManagement>.*?</pluginManagement>", "", body, flags=re.S)
    body = re.sub(r"<profiles>.*?</profiles>", "", body, flags=re.S)
    return [b for b in re.findall(r"<plugin>(?:(?!</plugin>).)*</plugin>", body, flags=re.S)
            if re.search(r"<artifactId>\s*spring-boot-maven-plugin\s*</artifactId>", b)]


def repackaged_dependency_lines(poms: list[PomInfo], raw: dict[str, RawPom]) -> list[str]:
    """v2.1v (D-M10): 다른 모듈이 의존하는 jar 모듈이 spring-boot-maven-plugin repackage 를 분류자 없이 돈다 — 클래스가
    `BOOT-INF/` 아래로 가서 의존하는 모듈이 "package does not exist" 로 깨진다(critical). `<classifier>` / `<skip>` 이 있으면
    괜찮다.

    A jar module other modules depend on runs spring-boot-maven-plugin repackage without a classifier (critical)."""
    by_key = {f"{p.group_id}:{p.artifact_id}": p for p in poms if p.group_id}
    dependents: dict[str, list[str]] = {}
    for p in poms:
        for d in (raw[p.path].raw.deps if p.path in raw else []):
            g = d.group_id
            if g in ("${project.groupId}", "${groupId}", ""):
                g = p.group_id or ""
            q = by_key.get(f"{g}:{d.artifact_id}")
            if q is not None and q.path != p.path and (d.type or "jar") == "jar":
                dependents.setdefault(q.path, []).append(p.path)
    out: list[str] = []
    for path, deps in dependents.items():
        q = next((p for p in poms if p.path == path), None)
        rp = raw.get(path)
        if q is None or rp is None or q.packaging not in ("jar", "") or not q.has_spring_boot_plugin:
            continue
        blocks = _boot_plugin_blocks(rp.text)
        if not blocks or any(re.search(r"<classifier>\s*\S", b) or re.search(r"<skip>\s*true\s*</skip>", b)
                             for b in blocks):
            continue
        out.append(f"- repackaged module used as a dependency: `{path}` (`{q.group_id}:{q.artifact_id}`) runs "
                   "spring-boot-maven-plugin repackage without a classifier and "
                   + ", ".join(f"`{x}`" for x in sorted(set(deps))[:8])
                   + " depend on it — its classes move under BOOT-INF/ (dependents: \"package does not exist\")")
    return out


# ---------------------------------------------------------------------------- invariants after the review's edits (v2.1y)
def _declared_versions(rp: Any) -> dict[tuple[str, str], Optional[str]]:
    """(구역, g:a) → 원시 `<version>` (import BOM 제외) — 구역은 dependencies / dependencyManagement."""
    out: dict[tuple[str, str], Optional[str]] = {}
    for section, deps in (("dependencies", rp.deps), ("dependencyManagement", rp.managed)):
        for d in deps:
            if (d.scope or "").lower() == "import":
                continue
            key = (section, f"{d.group_id}:{d.artifact_id}")
            out[key] = d.version or out.get(key)
    return out


def _declared_keys(rp: Any) -> set[str]:
    return {f"{d.group_id}:{d.artifact_id}" for d in [*rp.deps, *rp.managed]}


def invariant_violations(cfg: Any, changed: Iterable[str], before_poms: list[PomInfo], before: dict[str, Any],
                         after_poms: list[PomInfo], after: dict[str, Any], facts: Any = None,
                         results: Iterable[Any] = ()) -> list[str]:
    """v2.1y (D-M4): 검토의 편집이 재구성의 결정을 거꾸로 되돌렸는가 — 바뀐 pom 마다 (a) 회사 체인이 정의한 버전 속성을
    `<properties>` 에 새로 / 다른 값으로 적음, (b) 체인(또는 재구성이 import 한 Boot BOM)이 관리하는 아티팩트에 전에 없던
    명시 `<version>`, (c) 설정한 사내 부모도 프로젝트 안 pom 도 아닌 `<parent>`, (d) 필수 사내 아티팩트 선언을 지움. 위반마다
    한 문장(없으면 []). `before` / `after` = 경로 → RawPom. 예외 없음(검사 실패 = 위반 없음).

    Did the review's edits undo the restructure's decisions? One sentence per violation in the changed poms: (a) a
    `<properties>` entry the company chain defines (a `*.version` name) added or changed, (b) an explicit `<version>`
    on a chain- / Boot-BOM-managed artifact that had none, (c) a `<parent>` that is neither a configured company parent
    nor a pom of the project, (d) a mandatory dependency removed. Never raises."""
    try:
        return _invariant_violations(cfg, list(changed), before_poms, before, after_poms, after, facts,
                                     list(results or ()))
    except Exception:  # the check is a guard, never a failed review
        log.debug("review invariant check failed", exc_info=True)
        return []


def _invariant_violations(cfg: Any, changed: list[str], before_poms: list[PomInfo], before: dict[str, Any],
                          after_poms: list[PomInfo], after: dict[str, Any], facts: Any,
                          results: list[Any]) -> list[str]:
    a = cfg.artifacts
    cprops, csrc = company_properties(facts, cfg)
    vprops = {k: v for k, v in cprops.items() if is_version_property(k)}
    managed, by = managed_versions(cfg, results, facts)
    skip = _skip_keys(cfg, before_poms, results)
    project_keys = {f"{p.group_id}:{p.artifact_id}" for p in [*before_poms, *after_poms] if p.group_id}
    parents = {s.key: s.version for s in a.parent_specs()}
    aliases = set(a.parent_keys()) if hasattr(a, "parent_keys") else set(parents)
    mandatory = [s.key for s in a.mandatory or []]
    out: list[str] = []
    for path in [c for c in changed if posixpath.basename(c) == "pom.xml"]:
        b = before.get(path)
        f = after.get(path)
        braw = b.raw if b is not None else None
        fraw = f.raw if f is not None else None
        if fraw is not None:
            bprops = dict(braw.properties) if braw is not None else {}
            for name, value in (fraw.properties or {}).items():
                if name in vprops and bprops.get(name) != value:
                    out.append(f"`{path}`: property {name}={value} written — the parent chain defines {name} "
                               f"({vprops[name]}, {csrc.get(name) or 'the parent chain'}); reference it as ${{{name}}} "
                               "instead of defining it")
            bver = _declared_versions(braw) if braw is not None else {}
            for (section, key), v in _declared_versions(fraw).items():
                if not v or bver.get((section, key)) or key in skip or key in project_keys or key not in managed:
                    continue
                ref = _PROP_REF.match(v.strip())
                if ref and ref.group(1) in cprops:
                    continue
                out.append(f"`{path}`: explicit version {v} on {key} ({section}) — {by.get(key, 'the parent chain')} "
                           f"manages it at {managed[key]}; leave the version out")
            par, bpar = fraw.parent, braw.parent if braw is not None else None
            if par is not None and (bpar is None or (par.key, par.version) != (bpar.key, bpar.version)):
                ok = (par.key in project_keys
                      or (par.key in aliases and (parents.get(par.key) in (None, par.version))))
                if not ok:
                    out.append(f"`{path}`: parent {par.key}:{par.version or '?'} is neither a configured company "
                               "parent nor a pom of the project")
        if braw is not None:
            gone = [k for k in mandatory if k in _declared_keys(braw)
                    and (fraw is None or k not in _declared_keys(fraw))]
            out += [f"`{path}`: mandatory dependency {k} removed" for k in gone]
    return out


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
    """재구성 뒤 워크스페이스에서 검토 프롬프트에 들어갈 것을 전부 읽어 ReviewInput 으로 만든다(읽기만 한다). v2.1v:
    부모 체인의 버전 사실(`version_fact_lines`), 컨테이너 API 의 compile 스코프, 다른 모듈이 의존하는 repackage 모듈.

    Everything the review prompt shows, read from the workspace after the restructure (see module docstring)."""
    from sbmig.analyze.pom_parser import parse_poms

    results = [r for r in (results or ()) if r is not None]
    poms, warnings = parse_poms(ws)
    name = _project_name(cfg, poms, profile)
    inp = ReviewInput(pom_infos=poms, warnings=list(warnings),
                      language=str(getattr(getattr(cfg, "report", None), "language", "en") or "en").lower(),
                      ask_script=bool(getattr(getattr(cfg, "review", None), "ask_script", False)))
    facts = chain_facts(cfg)
    raw = raw_poms(ws, poms)
    inp.project = project_lines(profile, cfg, name)
    inp.rules, inp.external_parent = rule_lines(cfg, profile, name, results, facts)
    inp.restructure = restructure_lines(notes, results)
    inp.modules = module_lines(poms, ws)
    inp.facts = fact_lines(poms, cfg) + version_fact_lines(poms, cfg, results, facts=facts, raw=raw) \
        + container_scope_lines(raw) + repackaged_dependency_lines(poms, raw) + filtered_binary_lines(ws, poms) \
        + [f"- pom parse warning: {_cap(w)}" for w in warnings[:10]]
    inp.encodings = source_encodings(ws, poms, getattr(cfg.build, "encoding_candidates", None),
                                     skip_tests=bool(getattr(cfg.build, "skip_test_compile", True)))
    inp.poms, inp.omitted_poms = select_poms(ws, poms, int(cfg.review.max_poms), int(cfg.review.max_pom_chars))
    inp.chain, inp.raw, inp.results = facts, raw, results
    return inp
