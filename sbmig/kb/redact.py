"""KB 가 저장하는 모든 것의 비밀값 마스킹(round 10): Maven 원문, 메시지, diff, 스크립트 로그·코드, 구조 검토.
누가 부르나: ``sbmig.errors.common.Cleaner``(오류 목록·KB 의 모든 텍스트), ``CaseStore.add_script``(비밀값이
든 스크립트는 마스킹 후 retired). 값만(``***``), 그리고 문맥이 비밀이라고 말할 때만 가린다: 키의 마지막 단어가
비밀 단어인 ``key = value``, 그런 이름에 대입한 문자열 리터럴, XML·JSON 의 password 류, URL·JDBC 사용자 정보,
Authorization 헤더·Bearer 토큰, PEM 개인키. 코드는 가리지 않는다(``token = helper.clientIp();``, 비교식, for
문) — diff 와 스크립트가 읽을 수 있어야 하기 때문이다. 자리표시자(``${…}``, ``@…@`` 등)도 그대로 둔다. 회사
추가 규칙은 ``kb.redact_file``(``knowledge/kb/redact.yml``, 불변조건 2: 회사 특수 사항은 설정에)에서 읽어 기본
규칙에 더한다. 순수 함수이며 예외를 내지 않는다 — 마스킹 실패가 실행을 깨면 안 된다.

Secret masking for everything the KB stores (round 10, 2026-10-26): raw Maven text, messages, diffs, fix-script
logs and code, structure reviews.

Only VALUES are masked (``***``), and only where the context says they are secrets:

* ``key = value`` / ``key: value`` (properties, yaml, env, ``-Dkey=value``, log text) when the key's LAST word is a
  secret word (``password``, ``passwd``, ``pwd``, ``pw``, ``pass``, ``secret``, ``token``, ``credential(s)``,
  ``authorization``, ``api key``, ``private key``, …; words are split at ``.`` ``_`` ``-`` and camelCase, so
  ``spring.datasource.password``, ``db.pw``, ``userPw``, ``DB_PASSWORD`` are secrets and ``API_KEY_HEADER``,
  ``tokenCount``, ``password.encoder`` are not); at a line start the value runs to the end of the line, elsewhere
  to the next blank / quote / ``&`` / ``;``;
* a Java / XML-attribute / JS string literal assigned to such a name (``String password = "x";``,
  ``password="x"``) — never a non-literal expression (``String token = helper.clientIp();``), never a comparison
  (``secret == null``, ``token != x``), never ``for (String token : tokens)`` / ternaries;
* XML ``<password>…</password>``, ``<property name="password" value="…"/>``, ``<param-name>…password</param-name>
  <param-value>…</param-value>``; JSON / JS ``"password": "…"``;
* URL user info (``https://user:pass@host``), JDBC ``jdbc:oracle:thin:user/pass@…`` and ``;password=…`` /
  ``?password=…`` / ``&password=…`` parameters; ``Authorization: …`` headers and ``Bearer`` / ``Basic`` tokens;
  PEM private keys.

Placeholders (``${…}``, ``#{…}``, ``@…@``, ``__SET_ME__``, ``<…>``, ``***``) are left as they are. Company additions
(extra key words, whole key names, free patterns, allowed keys) come from ``kb.redact_file``
(``knowledge/kb/redact.yml``, invariant 2) and are added to the built-ins.

:func:`redact` masks a text; :meth:`Redactor.spans` gives the masked spans (the change classifier drops replace
pairs that touch one); :meth:`Redactor.redact_ex` also returns the masked values. Pure, never raises.


"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

log = logging.getLogger("sbmig.kb")

MASK = "***"
DEFAULT_RULES_FILE = "knowledge/kb/redact.yml"

# 키의 '마지막' 단어로 판단한다: password.encoder, tokenCount
# 처럼 비밀 단어가 앞에 있는 키는 설정 이름이지 비밀이 아니다
# the LAST word of a key that makes its value a secret (and two-word endings)
SECRET_WORDS = frozenset({
    "password", "passwd", "passphrase", "passcode", "pwd", "pw", "pass", "secret", "secrets", "token",
    "credential", "credentials", "authorization", "apikey", "privatekey", "secretkey", "accesskey", "authkey",
    "clientsecret", "sessionkey",
})
SECRET_PAIRS = frozenset({
    ("api", "key"), ("private", "key"), ("secret", "key"), ("access", "key"), ("auth", "key"), ("signing", "key"),
    ("encryption", "key"), ("session", "key"), ("master", "key"), ("client", "secret"),
})

_WORD_SPLIT_RE = re.compile(r"[._\-$:/\[\]]+")
_WORD_RE = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+")
_PLACEHOLDER_RE = re.compile(
    r"^(?:\$\{[^{}]*\}|#\{[^{}]*\}|@[\w.\-]+@|%[\w.\-]+%|__[A-Z_]+__|\*+|<[^<>]*>|\{\{[^{}]*\}\}|null|none|"
    r"\$[A-Za-z_]\w*|\$\([^()]*\))$", re.IGNORECASE)
_LINE_START_RE = re.compile(r"^[+\- ]?[ \t]*(?:-[ \t]+)?$")       # diff prefix, indentation, a yaml list marker
_LINE_COMMENT_RE = re.compile(r"\s+//.*$")
_YAML_COMMENT_RE = re.compile(r"\s+#.*$")
_CODE_CHARS_RE = re.compile(r"[\w$.()\[\]\s+\-*/%<>=!&|?:,\"']+")
_CALLISH_RE = re.compile(r"^(?:new\s+)?[A-Za-z_$][\w$.]*\s*(?:<[^<>]*>)?\s*\(.*\)[\w$.()\s]*$")
_TOKENISH_RE = re.compile(r"[0-9=+/_~\-]|\.(?=.)")

# ---- the built-in contexts (each yields the span of the VALUE to mask)
_PEM_RE = re.compile(r"-----BEGIN ([A-Z0-9 ]*?)PRIVATE KEY-----(?P<v>.+?)(?:-----END \1PRIVATE KEY-----|\Z)", re.S)
_URL_USERINFO_RE = re.compile(r"\b[a-zA-Z][a-zA-Z0-9+.\-]*://[^\s/:@'\"<>]+:(?P<v>[^\s/@'\"<>]+)@")
_JDBC_ORACLE_RE = re.compile(r"(?i)\bjdbc:oracle:[a-z]+:@?[^\s/@'\"<>:]+/(?P<v>[^\s@'\"<>]+)@")
_PARAM_RE = re.compile(r"(?<=[;?&])[ \t]*(?P<k>[A-Za-z_][\w.\-]*)[ \t]*=[ \t]*(?P<v>[^;&\s'\"<>]+)")
_BEARER_RE = re.compile(r"(?i)\b(?:Bearer|Basic|Digest)\s+(?P<v>[A-Za-z0-9._~+/=\-]{8,})")
_AUTH_HEADER_RE = re.compile(
    r"(?i)\b(?:Proxy-)?Authorization[ \t]*[:=][ \t]*(?:(?:Bearer|Basic|Digest|Token|Negotiate)[ \t]+)?"
    r"(?P<v>[^\s'\"<>,;]+)")
_XML_ELEM_RE = re.compile(r"<(?P<tag>(?:[\w\-]+:)?(?P<k>[\w.\-]+))(?:\s[^<>]*)?>(?P<v>[^<>]+)</(?P=tag)\s*>")
_XML_NV_RE = re.compile(
    r"""\b(?:name|key|property)\s*=\s*(?P<q1>["'])(?P<k>[^"'<>]+)(?P=q1)"""
    r"""(?:\s+[\w:.\-]+\s*=\s*(?:"[^"]*"|'[^']*'))*?\s+value\s*=\s*(?P<q2>["'])(?P<v>[^"'<>]*)(?P=q2)""")
_XML_PARAM_RE = re.compile(
    r"<(?P<t1>param-name|name|key)>\s*(?P<k>[\w.\-]+)\s*</(?P=t1)>[\s+\-]*?<(?P<t2>param-value|value)>"
    r"(?P<v>[^<>]*)</(?P=t2)>")
# the same value again right after it: a diff shows a changed `<param-value>` twice (the - and the + line)
_XML_PARAM_NEXT_RE = re.compile(r"[\s+\-]*?<(?P<t2>param-value|value)>(?P<v>[^<>]*)</(?P=t2)>")
_JSON_RE = re.compile(
    r"""(?P<q>["'])(?P<k>[\w.\-]+)(?P=q)\s*:\s*(?P<q2>["'])(?P<v>(?:\\.|(?!(?P=q2))[^\\\n])*)(?P=q2)""")
_ASSIGN_LIT_RE = re.compile(
    r"""(?:(?<=^-)|(?<![\w.$\-]))(?P<k>[A-Za-z_$][\w.$\-]*)[ \t]*=(?!=)[ \t]*(?P<q>["'])"""
    r"""(?P<v>(?:\\.|(?!(?P=q))[^\\\n])*)(?P=q)""", re.MULTILINE)
# a key starts after a non-key character, after `-D` (system properties) or after the `-` of a removed diff line
_ASSIGN_RE = re.compile(
    r"(?:(?<=-D)|(?<=^-)|(?<![\w.$\-]))(?P<k>[A-Za-z_$][\w.$\-]*)(?P<sp>[ \t]*)"
    r"(?P<sep>(?<![=!<>:+\-*/%&|^])[=:](?![=:]))[ \t]*", re.MULTILINE)
_MID_VALUE_RE = re.compile(r"[^\s\"'<>&;,(){}\[\]]+")


def key_words(key: str) -> list[str]:
    """키를 단어로 나눈다(``.`` ``_`` ``-`` 와 camelCase
    경계): ``userPw`` → [user, pw]; 끝의 숫자 단어는 버린다.

    ``spring.datasource.password`` → [spring, datasource, password]; ``userPw`` → [user, pw]; trailing
    number words dropped (``PASSWORD2`` → [password])."""
    words: list[str] = []
    for part in _WORD_SPLIT_RE.split(key or ""):
        words += [w.lower() for w in _WORD_RE.findall(part)]
    while words and words[-1].isdigit():
        words.pop()
    return words


@dataclass
class _Extra:
    """회사 추가 규칙(redact.yml): 비밀 단어, 키 전체 패턴, 자유 패턴, 허용 키."""

    words: frozenset = frozenset()
    key_patterns: list = field(default_factory=list)       # compiled, full match, case-insensitive
    patterns: list = field(default_factory=list)           # (name, compiled)
    allow: frozenset = frozenset()


class Redactor:
    """기본 규칙 + 회사 추가 규칙(:func:`load_rules`)으로 비밀값을 찾아 가린다.

    Built-in rules + company additions (:func:`load_rules`)."""

    def __init__(self, extra: Optional[_Extra] = None):
        self.extra = extra or _Extra()

    # ------------------------------------------------------------------ keys
    def is_secret_key(self, key: str) -> bool:
        """이 키의 값이 비밀인가: 허용 키는 아님, 회사 키 패턴은 비밀, 아니면 마지막
        단어(또는 마지막 두 단어)가 비밀 단어인지."""
        k = (key or "").strip().strip("\"'")
        if not k or k.lower() in self.extra.allow:
            return False
        if any(rx.fullmatch(k) for rx in self.extra.key_patterns):
            return True
        words = key_words(k)
        if not words:
            return False
        last = words[-1]
        if last in SECRET_WORDS or last in self.extra.words:
            return True
        return len(words) >= 2 and (words[-2], last) in SECRET_PAIRS

    # ------------------------------------------------------------------ spans
    def spans(self, text: Optional[str]) -> list[tuple[int, int]]:
        """``text`` 에서 가릴 구간(값만), 겹치는 것은 합쳐서 정렬.

        Merged, sorted spans of ``text`` to mask (values only)."""
        if not text:
            return []
        try:
            out: list[tuple[int, int]] = []
            self._collect(text, out)
            return _merge(out)
        except Exception as e:  # pragma: no cover - masking must never break a run
            # 규칙이 고장 나면 비밀 단어가 보이는 텍스트는 통째로 가린다 — 새는 것보다 덜 보이는 게 낫다
            log.debug("redaction failed: %s: %s", type(e).__name__, e)
            return [(0, len(text))] if any(w in text.lower() for w in ("password", "secret", "token")) else []

    def _collect(self, text: str, out: list[tuple[int, int]]) -> None:
        """모든 문맥 규칙을 돌며 가릴 값 구간을 ``out`` 에 모은다(자리표시자 값은 건너뜀)."""
        def add(m: re.Match, group: str = "v") -> None:
            s, e = m.span(group)
            v = text[s:e]
            stripped = v.strip()
            if not stripped or _PLACEHOLDER_RE.match(stripped):
                return
            lead = len(v) - len(v.lstrip())
            out.append((s + lead, s + lead + len(stripped)))

        for m in _PEM_RE.finditer(text):
            add(m)
        for rx in (_URL_USERINFO_RE, _JDBC_ORACLE_RE):
            for m in rx.finditer(text):
                add(m)
        for m in _PARAM_RE.finditer(text):
            if self.is_secret_key(m.group("k")):
                add(m)
        for m in _BEARER_RE.finditer(text):
            if _TOKENISH_RE.search(m.group("v")):
                add(m)
        for m in _AUTH_HEADER_RE.finditer(text):
            add(m)
        for rx in (_XML_ELEM_RE, _XML_NV_RE, _XML_PARAM_RE, _JSON_RE, _ASSIGN_LIT_RE):
            for m in rx.finditer(text):
                if self.is_secret_key(m.group("k")):
                    add(m)
                    # diff 에서는 바뀐 <param-value> 가 - 줄과 + 줄로
                    # 두 번 나오므로 바로 뒤의 값들(최대 4개)도 가린다
                    if rx is _XML_PARAM_RE:
                        pos = m.end()
                        for _ in range(4):
                            nm = _XML_PARAM_NEXT_RE.match(text, pos)
                            if nm is None:
                                break
                            add(nm)
                            pos = nm.end()
        for m in _ASSIGN_RE.finditer(text):
            if self.is_secret_key(m.group("k")):
                span = self._assign_value(text, m)
                if span is not None:
                    out.append(span)
        for _name, rx in self.extra.patterns:
            for m in rx.finditer(text):
                if "value" in rx.groupindex and m.group("value") is not None:
                    add(m, "value")
                elif m.end() > m.start():
                    out.append(m.span())

    @staticmethod
    def _assign_value(text: str, m: re.Match) -> Optional[tuple[int, int]]:
        """따옴표 없는 ``key = value`` / ``key: value`` 의 값 구간; 코드(메서드 호출·다른 변수)·빈
        값·자리표시자면 None — 코드까지 가리면 diff 를 읽을 수 없게 된다.

        The value span of an unquoted ``key = value`` / ``key: value``, None when it is code / empty / a
        placeholder."""
        sep, sp = m.group("sep"), m.group("sp")
        if sep == ":" and sp:
            return None                                  # `for (String token : tokens)`, `cond ? token : x`
        start = m.end()
        eol = text.find("\n", start)
        eol = len(text) if eol < 0 else eol
        after = text[start:eol].rstrip("\r")
        if not after.strip() or after[:1] in "\"'":
            return None                                  # nothing / a literal (the literal rule masks it)
        line_start = text.rfind("\n", 0, m.start("k")) + 1
        at_line_start = bool(_LINE_START_RE.match(text[line_start:m.start("k")]))
        mv = _MID_VALUE_RE.match(after)
        if mv is None:
            return None
        nxt = after[mv.end():mv.end() + 1]
        if nxt in ("\"", "'"):                         # `"…password=abc"`: inside a string literal
            v_end = start + mv.end()
        elif _code_rhs(after):
            return None                                  # `token = helper.clientIp();`, `password = other;`
        elif at_line_start:
            body = _YAML_COMMENT_RE.sub("", after) if sep == ":" else after
            v_end = start + len(body.rstrip())
        else:
            v_end = start + mv.end()
        value = text[start:v_end].strip()
        if not value or _PLACEHOLDER_RE.match(value.strip("\"'")):
            return None
        return start, v_end

    # ------------------------------------------------------------------ masking
    def redact_ex(self, text: Optional[str]) -> tuple[Optional[str], list[str]]:
        """(가린 텍스트, 가린 값들 순서대로). ``None`` 은 ``None``.

        (masked text, the masked values in order). ``None`` stays ``None``."""
        if text is None:
            return None, []
        spans = self.spans(text)
        if not spans:
            return text, []
        parts: list[str] = []
        values: list[str] = []
        pos = 0
        for s, e in spans:
            parts.append(text[pos:s])
            parts.append(MASK)
            values.append(text[s:e])
            pos = e
        parts.append(text[pos:])
        return "".join(parts), values

    def redact(self, text: Optional[str]) -> Optional[str]:
        """가린 텍스트만(``redact_ex`` 의 첫 값)."""
        return self.redact_ex(text)[0]


def _code_rhs(after: str) -> bool:
    """``key = …`` 의 오른쪽이 설정값이 아니라 코드(Java / JS 문장)처럼 보이나.

    The right-hand side of `key = …` reads like code (a Java / JS statement), not a configured value."""
    r = _LINE_COMMENT_RE.sub("", after).strip()
    if not r:
        return True
    if r.endswith("{"):
        return True
    if r.endswith(";"):
        body = r[:-1].strip()
        return not body or bool(_CODE_CHARS_RE.fullmatch(body) and re.match(r"(?:new\s+)?[A-Za-z_$(]", body))
    return bool(_CALLISH_RE.match(r))


def _merge(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for s, e in sorted(x for x in spans if x[1] > x[0]):
        if out and s <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def overlaps(spans: Iterable[tuple[int, int]], start: int, end: int) -> bool:
    """구간들 중 [start, end) 와 겹치는 것이 있나."""
    return any(s < end and start < e for s, e in spans)


# --------------------------------------------------------------------------- company rules (yml)
_CACHE: dict[str, tuple[Any, Redactor]] = {}
_DEFAULT = Redactor()


def _rules_path(cfg: Any = None) -> Optional[Path]:
    """``kb.redact_file`` 경로; 설정에 ""(빈 값)로 적으면 끈 것(None), 설정이 없으면 패키지의 기본 파일."""
    try:
        kbc = getattr(cfg, "kb", None)
        rel = getattr(kbc, "redact_file", None) if kbc is not None else None
        if cfg is not None and rel:
            return Path(cfg.resolve_path(rel))
        if cfg is not None and kbc is not None and rel == "":
            return None                                   # explicitly switched off
        from sbmig.core.config import PACKAGE_ROOT

        return PACKAGE_ROOT / DEFAULT_RULES_FILE
    except Exception:  # pragma: no cover - a stand-in cfg
        return None


def load_rules(path: Optional[Path]) -> _Extra:
    """redact yml 의 회사 추가 규칙(파일이 없거나 못 읽으면 없음; 잘못된 정규식은 경고 후 건너뜀).

    The company additions of a redact yml (missing / unreadable file → none; a bad regex is skipped)."""
    if path is None or not Path(path).is_file():
        return _Extra()
    try:
        import yaml

        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except Exception as e:
        log.warning("redaction rules %s unreadable (%s) — built-in rules only", path, e)
        return _Extra()
    if not isinstance(data, dict):
        return _Extra()

    def strings(key: str) -> list[str]:
        v = data.get(key) or []
        return [str(x) for x in v if isinstance(x, (str, int))] if isinstance(v, list) else []

    key_patterns, patterns = [], []
    for p in strings("key_patterns"):
        try:
            key_patterns.append(re.compile(p, re.IGNORECASE))
        except re.error as e:
            log.warning("redaction rules %s: bad key pattern %r: %s", path, p, e)
    for i, item in enumerate(data.get("patterns") or []):
        rx_text = item.get("regex") if isinstance(item, dict) else item if isinstance(item, str) else None
        if not rx_text:
            continue
        name = str(item.get("name") or f"pattern-{i + 1}") if isinstance(item, dict) else f"pattern-{i + 1}"
        try:
            patterns.append((name, re.compile(str(rx_text))))
        except re.error as e:
            log.warning("redaction rules %s: bad pattern %s %r: %s", path, name, rx_text, e)
    words = frozenset(w.lower().replace("_", "").replace("-", "") for w in strings("key_words"))
    return _Extra(words=words, key_patterns=key_patterns, patterns=patterns,
                  allow=frozenset(k.lower() for k in strings("allow_keys")))


def get_redactor(cfg: Any = None) -> Redactor:
    """``cfg`` 용 마스커(기본 규칙 + ``kb.redact_file``), 파일의
    수정 시각·크기로 캐시 — 파일을 고치면 재시작 없이 반영된다.

    The redactor for ``cfg`` (built-ins + its ``kb.redact_file``), cached by the file's stamp."""
    path = _rules_path(cfg)
    if path is None:
        return _DEFAULT
    key = str(path)
    try:
        st = path.stat()
        stamp: Any = (st.st_mtime_ns, st.st_size)
    except OSError:
        stamp = None
    cached = _CACHE.get(key)
    if cached is not None and cached[0] == stamp:
        return cached[1]
    red = Redactor(load_rules(path)) if stamp is not None else _DEFAULT
    _CACHE[key] = (stamp, red)
    return red


def redact(text: Optional[str], cfg: Any = None) -> Optional[str]:
    """``text`` 의 비밀값을 가린다(모듈 설명 참고); ``None`` 은 ``None``.

    Mask the secret values in ``text`` (see the module doc); ``None`` stays ``None``."""
    if not text:
        return text
    return get_redactor(cfg).redact(text)


def clear_cache() -> None:
    """마스커 캐시를 비운다(다음 ``get_redactor`` 가 규칙 파일을 다시 읽음)."""
    _CACHE.clear()
