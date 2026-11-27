"""JSP와 JS 파일에서 화면 주소와 REST 호출 주소를 읽어 모으는 모듈.

Contract: docs/FW-CONVERT.md section 2.5 "Page routes and REST calls", record ``FrontendApiMap``.

- ``.mvc`` URLs (``frontend.page_suffix``): every occurrence in a JSP or in a JS file of at most
  ``frontend.js_scan_limit_kb`` KB, comments blanked first (JS comments by the lexer; in a JSP the JSP comments, the
  HTML comments outside script blocks and the JS comments inside them, see ``jsp_parts``). The text is cut into runs
  of path characters (``[A-Za-z0-9_\\-./]``), so a context-path expression (``${…}``, ``<%= … %>``), a quote, ``+``
  or ``=`` ends a URL; inside a run a URL ends after each ``.mvc`` that is not followed by a word character, and the
  next one starts there. A URL longer than ``_MAX_URL`` characters, a ``//host`` URL and a name that is not
  ``[A-Za-z0-9_-]+`` are ignored. Each occurrence gives a page route (``route_name`` of the file-name part) and its
  spelling in ``page_urls`` exactly as written (context path removed); only an absolute URL (starting with ``/``)
  votes for ``mvc_url_prefix`` with the path in front of the name (``.`` / ``..`` segments dropped). A relative URL
  does not vote; the Java generator resolves it against the prefix. ``frontend.route_exclude`` is compared with the
  route and with the lower-cased name; an excluded URL still votes. Most votes wins, ties: longest, then
  alphabetical.
- REST calls: ``api.get/post/put/del/delete/patch(url…)``, ``fetch(url, {method})`` (default GET),
  ``$.ajax({url, type|method})`` / ``jQuery.ajax`` / ``$.ajax(url, {type})`` (default GET), in JS files and in the
  script pieces of a JSP (script blocks, ``on*`` attribute values, ``javascript:`` URLs; ``jsp_parts``). A
  call whose argument list does not close within ``_MAX_CALL`` characters, or whose URL argument is longer than
  ``_MAX_ARG``, is skipped. The URL expression is normalised: literal parts kept, every other operand becomes
  ``{name}`` (its last identifier, ``param`` when there is none), ``${expr}`` of a template literal likewise; query
  and fragment cut; a leading context-path part before the API prefix dropped. Only ``api_prefix`` or
  ``api_prefix/…`` counts. One record per (verb, path), ``source`` = the first file in sorted order.
- JSP script text (script blocks, ``on*`` values, ``javascript:`` URLs; ``rest_calls(..., jsp=True)``): before
  lexing, every ``<% … %>``, ``${ … }`` and ``#{ … }`` span becomes one neutral operand of the same length (``0`` +
  spaces; ``_neutral``), so ``${total} / 2`` is a division and offsets stay; string contents, placeholder names and
  the context-path removal read the ORIGINAL text at the same offsets (``_Js.src``). An escaped ``\\${`` / ``\\#{``
  is not a server span: JSP prints ``${`` / ``#{``, so string contents read it that way (a template placeholder). A
  ``.js`` file is lexed as it is (``${``, ``<%`` are ordinary text there). An ``on*`` attribute value ends at the
  first quote outside those spans (``_attr_end``).
- Cost: one linear lexer pass per text (``_Js``) gives the bracket pairs and the string / comment / regex-literal
  spans; every later step jumps over them. A closing bracket that is not the partner of the innermost open one is
  ignored (O(1)). A string attempt reads its body first (``_STRING_BODY``) and checks the closing quote itself; a
  failed attempt is never retried with the same quote before the position where it stopped (a backslash-continued
  string can run to the end of the text). A regex literal is recognised after ``( , = : [ ! & | ? { } ;``,
  ``return`` / ``typeof`` / ``case`` and at the start; it cannot span lines, so a failed one stops regex detection to
  the end of the line.
- Work wall: every pass over one file spends from one ``_Budget`` of ``_WORK_FACTOR`` (32) x the text length +
  ``_WORK_SLACK``; when it runs out the file gives nothing (no REST call, no ``.mvc`` URL) and ``scan_skipped`` names
  it with ``TOO_MUCH_WORK``. Charging rule: whatever a step examines is charged, including a search that fails or
  travels far, by the distance travelled (never a constant). Searches that only move forward over one stretch are
  charged once for that stretch. Measured ordinary input stays at or below about 12x (EL-dense JSPs, libraries
  inlined in a script block); JS files about 6x. ``_api_path`` runs outside the budget on a URL of at most
  ``_MAX_ARG`` characters that ``_Js.url`` already charged. A file over ``frontcopy.scan_limit``
  (``frontend.max_scan_kb``), an unreadable file and a file whose real path leaves the source root are skipped too;
  ``scan_skipped`` names them for the report (it runs the scan again)."""
from __future__ import annotations

import html
import re
import stat
from collections import Counter
from pathlib import Path
from typing import Optional

from sbmig.fwconvert.frontcopy import _clean_rel, _inside, _walk, decode_source, scan_limit
from sbmig.fwconvert.model import FrontendApiMap, RestCall
from sbmig.fwconvert.naming import kebab
from sbmig.fwconvert.rules import ConvertRules

_MAX_URL = 2048
_MAX_CALL = 8192
_MAX_ARG = 2048
# 파일 하나를 읽는 데 쓸 수 있는 일의 양. 본문 길이의 몇 배와 여유분이다. 넘으면 그 파일은 읽지 않는다
_WORK_FACTOR = 32
_WORK_SLACK = 4096
TOO_MUCH_WORK = "scan stopped: too much work for its size"

_PATH_RUN = re.compile(r"[A-Za-z0-9_\-./]+")
_NAME = re.compile(r"[A-Za-z0-9_\-]+")
_WORD = re.compile(r"\w")
_IDENT = re.compile(r"[A-Za-z_$][\w$]*")
# 세 가지 호출을 한 번에 찾는다. api.<메서드>(, fetch(, $.ajax( 순서다
_CALL = re.compile(r"(?<![\w$.])api\s*\.\s*(?P<api>get|post|put|del|delete|patch)\s*\("
                   r"|(?P<fetch>(?:(?<![\w$.])|(?<=\bwindow\.))fetch)\s*\("
                   r"|(?P<ajax>(?<![\w$.])(?:\$|jQuery)\s*\.\s*ajax)\s*\(")
_KEY = re.compile(r"\s*(?:([A-Za-z_$][\w$]*)|(['\"])([^'\"\r\n]*)\2)\s*:")
_NEXT = re.compile(r"[\"'`/()\[\]{}]")
# 문자열의 본문만 읽는 식. 닫는 따옴표는 따로 확인한다. 실패해도 어디까지 읽었는지 알 수 있다
_STRING_BODY = {"'": re.compile(r"'(?:[^'\\\r\n]|\\.)*", re.S), '"': re.compile(r'"(?:[^"\\\r\n]|\\.)*', re.S),
                "`": re.compile(r"`(?:[^`\\]|\\.)*", re.S)}
_LINE_COMMENT = re.compile(r"//[^\r\n]*")
_BLOCK_COMMENT = re.compile(r"/\*(?:[^*]|\*(?!/))*(?:\*/|\Z)")
_REGEX = re.compile(r"/(?:[^/\\\r\n\[]|\\[^\r\n]|\[(?:[^\]\\\r\n]|\\[^\r\n])*\])+/[A-Za-z]*")
_REGEX_AFTER = frozenset("(,=:[!&|?{};")
_REGEX_WORDS = ("return", "typeof", "case")
_PAIR = {")": "(", "]": "[", "}": "{"}
_JSP_SPLIT = re.compile(r"<!--|<script\b", re.I)
_SCRIPT_END = re.compile(r"</script", re.I)
_HANDLER = re.compile(r"(?<![\w.:-])(on[A-Za-z]+|href)\s*=\s*([\"'])(\s*javascript:)?", re.I)
_SERVER_OPEN = re.compile(r"<%|\$\{|#\{")
_MAY_CALL = re.compile(r"api|fetch|ajax")
# 코드 포인트가 될 수 없을 만큼 긴 숫자 문자 참조. 그대로 둔다. int()는 아주 긴 숫자를 거부한다
_LONG_CHARREF = re.compile(r"&#(?:[xX][0-9A-Fa-f]{9,}|[0-9]{9,})")


class _TooMuchWork(Exception):
    """파일 하나를 읽는 데 정해 둔 양보다 많은 일을 했을 때 던지는 예외."""


class _Budget:
    """파일 하나를 읽는 동안 한 일의 양을 세는 클래스. 정해 둔 양을 넘으면 ``_TooMuchWork``를 던진다."""

    def __init__(self, size: int) -> None:
        self.left = _WORK_FACTOR * max(size, 0) + _WORK_SLACK

    def spend(self, n: int) -> None:
        self.left -= n
        if self.left < 0:
            raise _TooMuchWork(TOO_MUCH_WORK)


def route_name(name: str) -> str:
    """화면 이름으로 경로 이름을 만든다. 소문자가 없는 이름은 소문자로만 바꾼다.
    예: ``orderList``는 ``order-list``, ``AB1C000010``은 ``ab1c000010``."""
    return kebab(name) if re.search(r"[a-z]", name) else name.lower()


# ---- 파일 읽기 --------------------------------------------------------------


def _candidates(src_root: Path, files: Optional[list[str]]) -> list[str]:
    if files is None:
        return _walk(src_root) if src_root.is_dir() else []
    return sorted({c for c in (_clean_rel(f) for f in files) if c})


def _front_files(src_root: Path, rules: ConvertRules, files: Optional[list[str]]):
    """검사할 JSP, JS 파일을 (경로, 본문 또는 None, 건너뛴 이유)로 차례로 낸다."""
    limit = scan_limit(rules)
    for rel in _candidates(src_root, files):
        low = rel.lower()
        if not (low.endswith(".jsp") or low.endswith(".js")):
            continue
        path = src_root / rel
        if not _inside(path, src_root):
            yield rel, None, "resolves to a file outside the source folder"
            continue
        try:
            st = path.stat()
            # 이름 있는 파이프나 장치 파일은 읽으면 멈출 수 있다. 일반 파일만 읽는다
            if not stat.S_ISREG(st.st_mode):
                yield rel, None, "not a regular file"
                continue
            if st.st_size > limit:
                yield rel, None, f"larger than the scan limit of {limit // 1024} KB"
                continue
            raw = path.read_bytes()
            text = decode_source(raw)[0]
        except OSError as e:
            yield rel, None, f"cannot read ({e.__class__.__name__}: {e})"
            continue
        except Exception as e:  # noqa: BLE001 - 파일 하나의 문제가 전체 검사를 멈추지 않게 한다
            yield rel, None, f"scan failed: {e.__class__.__name__}"
            continue
        yield rel, text, ""


def _settings(rules: ConvertRules) -> tuple[str, int]:
    suffix = rules.text("frontend.page_suffix", ".mvc")
    try:
        limit = int(float(rules.get("frontend.js_scan_limit_kb", 512) or 0) * 1024)
    except (TypeError, ValueError):
        limit = 512 * 1024
    return suffix, limit


def scan_skipped(src_root: Path, rules: ConvertRules, files: Optional[list[str]] = None) -> list[str]:
    """``extract_api_map``이 읽지 않고 건너뛴 JSP, JS 파일을 "경로: 이유" 목록으로 반환한다.
    크기 제한을 넘은 파일, 읽지 못한 파일, 일반 파일이 아닌 것, 원본 폴더 밖을 가리키는 파일이다.
    읽는 데 일이 너무 많은 파일과 읽다가 예상하지 못한 오류가 난 파일도 넣는다."""
    suffix, limit = _settings(rules)
    out: list[str] = []
    for rel, text, why in _front_files(Path(src_root), rules, files):
        if text is None:
            out.append(f"{rel}: {why}")
            continue
        try:
            _scan_text(rel, text, suffix, limit)
        except _TooMuchWork:
            out.append(f"{rel}: {TOO_MUCH_WORK}")
        except Exception as e:  # noqa: BLE001 - extract_api_map과 같은 규칙으로 이 파일만 건너뛴다
            out.append(f"{rel}: scan failed: {e.__class__.__name__}")
    return out


# ---- .mvc 주소 --------------------------------------------------------------


def mvc_urls(text: str, suffix: str) -> list[tuple[str, str, str]]:
    """본문의 ``.mvc`` 주소를 모두 찾아 (화면 이름, 쓰여 있던 주소, 이름 앞 경로) 목록으로 반환한다.
    컨텍스트 경로 식은 주소에 넣지 않는다. 주석은 호출하는 쪽에서 미리 지운다.
    ``/``로 시작하는 주소만 세 번째 값이 있다. 상대 주소는 쓰여 있던 그대로 두고 세 번째 값은 빈 문자열이다.
    읽는 데 일이 너무 많으면 빈 목록을 반환한다."""
    try:
        return _mvc_urls(text, suffix, _Budget(len(text)))
    except Exception:  # noqa: BLE001 - 일의 양 제한과 같이 빈 목록이다
        return []


def _mvc_urls(text: str, suffix: str, budget: _Budget) -> list[tuple[str, str, str]]:
    out: list[tuple[str, str, str]] = []
    budget.spend(len(text))
    if not suffix or suffix not in text:
        return out
    prev = 0
    for run in _PATH_RUN.finditer(text):
        s, base, start = run.group(0), run.start(), 0
        # 앞 조각 끝에서 이 조각 끝까지 읽은 만큼 센다. 조각 안의 find는 앞으로만 가므로 조각 길이에 들어간다
        budget.spend(run.end() - prev)
        prev = run.end()
        at = s.find(suffix)
        while at >= 0:
            end = at + len(suffix)
            nxt = base + end
            if nxt < len(text) and _WORD.match(text, nxt):
                at = s.find(suffix, at + 1)
                continue
            url = s[start:end]
            start = end
            at = s.find(suffix, end)
            if len(url) > _MAX_URL or url.startswith("//"):
                continue
            budget.spend(len(url))
            head, _slash, last = url.rpartition("/")
            name = last[: -len(suffix)]
            if not name or not _NAME.fullmatch(name):
                continue
            prefix = ""
            if url.startswith("/"):
                segments = [p for p in head.split("/") if p and p not in (".", "..")]
                prefix = "/" + "/".join(segments) if segments else ""
            out.append((name, url, prefix))
    return out


def _blank(text: str, spans: list[tuple[int, int]]) -> str:
    """범위의 글을 같은 길이의 공백으로 바꾼다. 주석이 주소의 일부로 읽히지 않게 한다."""
    if not spans:
        return text
    pieces, pos = [], 0
    for a, b in spans:
        pieces.append(text[pos:a])
        pieces.append(" " * (b - a))
        pos = b
    pieces.append(text[pos:])
    return "".join(pieces)


def _server_spans(text: str, budget: _Budget) -> list[tuple[int, int]]:
    """JSP가 먼저 처리하는 ``<% %>``, ``${ }``, ``#{ }`` 범위를 반환한다. 닫히지 않은 것부터는 찾지 않는다."""
    spans: list[tuple[int, int]] = []
    pos = 0
    while True:
        m = _SERVER_OPEN.search(text, pos)
        if not m:
            budget.spend(len(text) - pos)
            break
        # \${ 와 \#{ 는 JSP가 처리하지 않고 ${, #{ 로 내보낸다. 브라우저의 템플릿 자리 표시다
        if m.group(0) != "<%" and m.start() > 0 and text[m.start() - 1] == "\\":
            budget.spend(m.end() - pos)
            pos = m.end()
            continue
        close = "%>" if m.group(0) == "<%" else "}"
        end = text.find(close, m.end())
        if end < 0:
            budget.spend(len(text) - pos)
            break
        end += len(close)
        budget.spend(end - pos)
        spans.append((m.start(), end))
        pos = end
    return spans


def _neutral(text: str, budget: _Budget) -> str:
    """JSP 식을 같은 길이의 피연산자 하나(``0``과 공백)로 바꾼다. 위치는 그대로다.
    ``${total} / 2``의 ``/``가 정규식이 아니라 나눗셈으로 읽히게 한다."""
    spans = _server_spans(text, budget)
    if not spans:
        return text
    budget.spend(len(text))
    pieces, pos = [], 0
    for a, b in spans:
        pieces.append(text[pos:a])
        pieces.append("0" + " " * (b - a - 1))
        pos = b
    pieces.append(text[pos:])
    return "".join(pieces)


# ---- JS 읽기 ----------------------------------------------------------------


class _Js:
    """본문을 한 번 읽어 괄호 짝과 문자열, 주석 범위를 기억하는 클래스.
    JSP에서 온 본문은 구조를 JSP 식을 지운 본문에서 읽고, 문자열 내용은 원래 본문에서 꺼낸다."""

    def __init__(self, text: str, budget: Optional[_Budget] = None, jsp: bool = False) -> None:
        self.budget = budget or _Budget(len(text))
        self.jsp = jsp
        self.src = text                                  # 문자열 내용과 이름을 꺼내는 원래 본문
        # 구조를 읽는 본문. JSP에서 온 본문은 JSP 식을 피연산자 하나로 바꾼다. JS 파일은 그대로 읽는다
        self.text = _neutral(text, self.budget) if jsp else text
        self.jump: dict[int, int] = {}      # 문자열, 주석, 정규식, 괄호의 시작 위치와 끝 다음 위치
        self.strings: set[int] = set()       # 문자열 리터럴이 시작하는 위치
        self.comments: list[tuple[int, int]] = []
        self._lex()

    def _lex(self) -> None:
        text, spend = self.text, self.budget.spend
        stack: list[tuple[str, int]] = []
        n = len(text)
        pos = floor = 0
        # 실패한 시도가 읽은 끝 위치. 그 앞에서는 같은 시도를 다시 하지 않는다
        no_regex_until = -1
        dead: dict[str, int] = {}
        while True:
            m = _NEXT.search(text, pos)
            if not m:
                spend(n - pos)
                break
            i = m.start()
            spend(i - pos + 1)
            c = text[i]
            end = i + 1
            if c in "'\"`":
                if i >= dead.get(c, -1):
                    body = _STRING_BODY[c].match(text, i).end()
                    spend(body - i)
                    if body < n and text[body] == c:
                        end = body + 1
                        self.jump[i] = end
                        self.strings.add(i)
                    else:
                        # 닫히지 않은 문자열이다. 이 시도가 읽은 곳까지는 같은 따옴표로 다시 시작하지 않는다
                        dead[c] = body
            elif c == "/":
                nxt = text[i + 1:i + 2]
                cm = _LINE_COMMENT.match(text, i) if nxt == "/" else _BLOCK_COMMENT.match(text, i) if nxt == "*" \
                    else None
                if cm:
                    end = cm.end()
                    spend(end - i)
                    self.jump[i] = end
                    self.comments.append((i, end))
                elif i > no_regex_until and self._regex_allowed(i, floor):
                    rm = _REGEX.match(text, i)
                    if rm:
                        end = rm.end()
                        spend(end - i)
                        self.jump[i] = end
                    else:
                        line_end = text.find("\n", i)
                        no_regex_until = n if line_end < 0 else line_end
                        spend(no_regex_until - i)
            elif c in "([{":
                stack.append((c, i))
            elif stack and stack[-1][0] == _PAIR[c]:
                # 닫는 괄호는 스택 맨 위가 짝일 때만 닫는다. 짝이 아니면 무시한다
                self.jump[stack.pop()[1]] = i + 1
            pos = floor = end

    def _regex_allowed(self, i: int, floor: int) -> bool:
        """``i``의 ``/``가 정규식 리터럴을 시작할 수 있는 자리면 True를 반환한다."""
        t = self.text
        j = i - 1
        while j >= floor and t[j].isspace():
            j -= 1
        self.budget.spend(i - j)
        if j < 0:
            return True
        if t[j] in _REGEX_AFTER:
            return True
        for word in _REGEX_WORDS:
            k = j + 1 - len(word)
            if k >= 0 and t.startswith(word, k) and (k == 0 or not (t[k - 1].isalnum() or t[k - 1] in "_$")):
                return True
        return False

    def blanked(self) -> str:
        """주석을 공백으로 바꾼 원래 본문을 반환한다."""
        self.budget.spend(len(self.src))
        return _blank(self.src, self.comments)

    def content(self, a: int, b: int) -> str:
        """``a``에서 ``b``까지 문자열 리터럴의 내용을 원래 본문에서 꺼낸다.
        JSP에서 온 본문은 ``\\${``를 브라우저가 받는 ``${``로 읽는다."""
        self.budget.spend(b - a)
        text = self.src[a + 1:b - 1]
        if self.jsp and "\\" in text:
            text = text.replace("\\${", "${").replace("\\#{", "#{")
        return text

    def _top_level(self, a: int, b: int, sep: str, want: int) -> list[tuple[int, int]]:
        """범위 안을 최상위 ``sep``로 나눈다. 문자열과 괄호 안은 건너뛴다. ``want``개를 채우면 멈춘다."""
        parts: list[tuple[int, int]] = []
        i, start, steps = a, a, 0
        while i < b and len(parts) < want:
            steps += 1
            j = self.jump.get(i)
            if j is not None:
                i = j
                continue
            if self.text[i] == sep:
                parts.append((start, i))
                start = i + 1
            i += 1
        self.budget.spend(steps)
        if len(parts) < want:
            parts.append((start, b))
        return parts

    def args(self, open_paren: int, want: int) -> Optional[list[tuple[int, int]]]:
        """``open_paren``의 괄호 안 인자를 앞에서부터 ``want``개까지 (시작, 끝) 범위로 반환한다.
        괄호가 닫히지 않았거나 너무 길면 None이다."""
        close = self.jump.get(open_paren, -1)
        if close < 0 or close - open_paren > _MAX_CALL or self.text[open_paren] != "(":
            return None
        return [self._trim(a, b) for a, b in self._top_level(open_paren + 1, close - 1, ",", want)]

    def _trim(self, a: int, b: int) -> tuple[int, int]:
        t, a0, b0 = self.text, a, b
        while a < b and t[a].isspace():
            a += 1
        while b > a and t[b - 1].isspace():
            b -= 1
        self.budget.spend((a - a0) + (b0 - b) + 1)
        return a, b

    def literal(self, a: int, b: int) -> Optional[str]:
        """범위 전체가 문자열 리터럴 하나면 그 내용을 반환한다. 아니면 None이다."""
        if a in self.strings and self.jump.get(a) == b:
            return self.content(a, b)
        return None

    def props(self, a: int, b: int) -> dict[str, tuple[int, int]]:
        """범위가 객체 리터럴이면 최상위 속성 이름과 값 범위를 반환한다."""
        if b - a < 2 or self.text[a] != "{" or self.jump.get(a) != b:
            return {}
        out: dict[str, tuple[int, int]] = {}
        for s, e in self._top_level(a + 1, b - 1, ",", b):
            # 속성 이름 식은 조각 끝까지 읽을 수 있다. 조각 길이를 센다
            self.budget.spend(e - s)
            m = _KEY.match(self.text, s, e)
            if m:
                out.setdefault(m.group(1) or m.group(3), self._trim(m.end(), e))
        return out

    def url(self, a: int, b: int) -> str:
        """범위의 URL 식을 경로 하나로 바꾼다. 문자열이 하나도 없으면 빈 문자열이다."""
        if b - a > _MAX_ARG:
            return ""
        # 범위를 한 번 걷고, 문자열 내용과 이름 조각을 한 번씩 더 읽는다
        self.budget.spend(b - a)
        t, src = self.text, self.src
        out: list[str] = []
        literal = False
        i = a
        while i < b:
            c = t[i]
            if c.isspace() or c == "+":
                i += 1
                continue
            if i in self.strings:
                end = self.jump[i]
                content = self.content(i, end)
                out.append(_template(content) if c == "`" else content)
                literal = True
                i = end
                continue
            # 문자열이 아닌 조각은 다음 + 까지 한 조각으로 보고 {이름}으로 바꾼다
            j = i
            while j < b and t[j] != "+":
                j = self.jump.get(j, j + 1)
            self.budget.spend(j - i)
            out.append(_placeholder(src[i:j]))
            i = j
        if not literal:
            return ""
        return re.split(r"[?#]", "".join(out), maxsplit=1)[0]


def _placeholder(expr: str) -> str:
    idents = [m.group(0) for m in _IDENT.finditer(re.sub(r"\([^()]*\)", "", expr))]
    return "{" + (idents[-1] if idents else "param") + "}"


def _template(content: str) -> str:
    out, i = [], 0
    while i < len(content):
        if content.startswith("${", i):
            end = content.find("}", i + 2)
            if end < 0:
                return ""
            out.append(_placeholder(content[i + 2:end]))
            i = end + 1
        else:
            nxt = content.find("${", i)
            nxt = len(content) if nxt < 0 else nxt
            out.append(content[i:nxt])
            i = nxt
    return "".join(out)


def normalise_url(expr: str) -> str:
    """URL 식을 경로 하나로 바꾼다. 문자열은 그대로 두고, 나머지 식은 ``{이름}``으로 바꾼다.
    예: ``'/api/x/' + id``와 ``\\`/api/x/${id}\\```는 ``/api/x/{id}``다. 쿼리 문자열은 잘라 낸다.
    문자열이 하나도 없으면 빈 문자열을 반환한다."""
    s = expr.strip()
    try:
        return _Js(s).url(0, len(s))
    except Exception:  # noqa: BLE001 - 일의 양 제한과 같이 빈 문자열이다
        return ""


def _api_path(path: str, prefix: str) -> str:
    """API 접두사로 시작하는 경로만 남긴다. 앞에 붙은 컨텍스트 경로 식은 뗀다. 아니면 빈 문자열이다."""
    def ok(i: int) -> bool:
        if not path.startswith(prefix, i):
            return False
        j = i + len(prefix)
        return j == len(path) or path[j] == "/"
    i = 0
    while not ok(i):
        # 앞의 {이름}, ${...}, <%= ... %> 조각을 하나씩 뗀다
        if path.startswith("{", i):
            end = path.find("}", i)
        elif path.startswith("${", i) or path.startswith("#{", i):
            end = path.find("}", i)
        elif path.startswith("<%", i):
            end = path.find("%>", i)
            end = end + 1 if end >= 0 else -1
        else:
            return ""
        if end < 0:
            return ""
        i = end + 1
    return path[i:]


def rest_calls(text: str, jsp: bool = False) -> list[tuple[str, str]]:
    """JS 본문의 REST 호출을 (HTTP 메서드, URL 식을 바꾼 경로) 목록으로 반환한다. 경로 접두사는 거르지 않는다.
    주석 안의 호출은 세지 않는다. 읽는 데 일이 너무 많으면 빈 목록을 반환한다.
    ``jsp``가 True면 JSP에서 꺼낸 스크립트로 읽는다. JSP 식은 피연산자 하나로 보고 ``\\${``는 ``${``로 읽는다."""
    budget = _Budget(len(text))
    try:
        if not _may_call(text, budget):
            return []
        return _calls(_Js(text, budget, jsp=jsp))
    except Exception:  # noqa: BLE001 - 일의 양 제한과 같이 빈 목록이다
        return []


def _may_call(text: str, budget: _Budget) -> bool:
    m = _MAY_CALL.search(text)
    budget.spend(m.end() if m else len(text))
    return m is not None


def _attr_end(t: str, start: int, q: str, budget: _Budget) -> int:
    """속성 값의 닫는 따옴표 위치를 반환한다. 안의 ``<% %>``, ``${ }``, ``#{ }``는 건너뛴다. 없으면 -1이다.

    JSP evaluates these spans before the HTML exists, so a quote inside one never ends the attribute. Linear: the
    candidate quote is searched once from where the last span ended; the spans in front of it are walked once; a
    quote search starts again only after a span that ran past the candidate. Every search is charged its distance."""
    pos = start
    qpos = t.find(q, pos)
    budget.spend((qpos if qpos >= 0 else len(t)) - pos + 1)
    while qpos >= 0:
        m = _SERVER_OPEN.search(t, pos, qpos)
        if not m:
            budget.spend(qpos - pos)
            return qpos
        # \${ 와 \#{ 는 JSP 식이 아니다. 그 뒤부터 다시 찾는다
        if m.group(0) != "<%" and m.start() > 0 and t[m.start() - 1] == "\\":
            budget.spend(m.end() - pos)
            pos = m.end()
            continue
        close = "%>" if m.group(0) == "<%" else "}"
        end = t.find(close, m.end())
        if end < 0:
            budget.spend(len(t) - pos)
            return -1
        end += len(close)
        budget.spend(end - pos)
        pos = end
        if pos > qpos:
            # 따옴표가 JSP 식 안에 있었다. 식이 끝난 곳부터 다음 따옴표를 찾는다
            qpos = t.find(q, pos)
            budget.spend((qpos if qpos >= 0 else len(t)) - pos + 1)
    return -1


def _unescape(value: str) -> str:
    """속성 값의 HTML 문자 참조를 푼다. 코드 포인트가 될 수 없을 만큼 긴 숫자 참조는 쓰여 있던 그대로 둔다."""
    if "&#" not in value or not _LONG_CHARREF.search(value):
        return html.unescape(value)
    pieces, pos = [], 0
    for m in _LONG_CHARREF.finditer(value):
        pieces.append(html.unescape(value[pos:m.start()]))
        pieces.append(m.group(0))
        pos = m.end()
    pieces.append(html.unescape(value[pos:]))
    return "".join(pieces)


def jsp_parts(text: str) -> tuple[str, list[str]]:
    """JSP 본문을 (주석을 공백으로 바꾼 본문, 스크립트 조각 목록)으로 나눠 반환한다.
    - JSP 주석과 스크립트 밖의 HTML 주석은 공백으로 바꾼다
    - 스크립트 블록 안은 JS 주석을 공백으로 바꾼다
    - 스크립트 조각은 스크립트 블록 본문, ``on*`` 속성 값, ``javascript:`` 주소다
    읽는 데 일이 너무 많으면 (원래 본문, 빈 목록)을 반환한다.

    An HTML comment inside a script block (the old ``<!-- … //-->`` wrapping) is script text, not a comment. Attribute
    values are HTML-unescaped; a JSP expression or EL inside one does not end it. An attribute whose closing quote is
    missing stops the search for that quote."""
    try:
        blanked, pieces = _jsp_parts(text, _Budget(len(text)))
        return blanked, [piece for piece, _js in pieces]
    except Exception:  # noqa: BLE001 - 일의 양 제한과 같이 (원래 본문, 빈 목록)이다
        return text, []


def _jsp_parts(text: str, budget: _Budget) -> tuple[str, list[tuple[str, Optional["_Js"]]]]:
    """``jsp_parts``와 같다. 스크립트 블록은 이미 읽은 ``_Js``를 함께 돌려준다. 속성 값은 None이다."""
    # JSP 주석 <%-- --%>은 서버에서 지워지므로 어디에 있든 먼저 지운다
    spans, pos = [], 0
    while True:
        a = text.find("<%--", pos)
        if a < 0:
            break
        b = text.find("--%>", a + 4)
        b = len(text) if b < 0 else b + 4
        spans.append((a, b))
        pos = b
    budget.spend(len(text))
    t = _blank(text, spans)
    out: list[str] = []
    scripts: list[tuple[str, Optional[_Js]]] = []
    pos = 0
    while True:
        m = _JSP_SPLIT.search(t, pos)
        if not m:
            budget.spend(len(t) - pos)
            break
        if m.group(0) == "<!--":
            end = t.find("-->", m.end())
            end = len(t) if end < 0 else end + 3
            budget.spend(end - pos)
            out.append(t[pos:m.start()])
            out.append(" " * (end - m.start()))
            pos = end
            continue
        tag_end = t.find(">", m.end())
        if tag_end < 0:
            budget.spend(len(t) - pos)
            break
        close = _SCRIPT_END.search(t, tag_end + 1)
        body_end = close.start() if close else len(t)
        budget.spend(body_end - pos)
        body = t[tag_end + 1:body_end]
        out.append(t[pos:tag_end + 1])
        js = _Js(body, budget, jsp=True)
        out.append(js.blanked())
        scripts.append((body, js))
        pos = body_end
    out.append(t[pos:])
    blanked = "".join(out)
    budget.spend(len(blanked))
    dead: set[str] = set()
    prev = value_end = 0
    for m in _HANDLER.finditer(blanked):
        # finditer가 앞 일치 끝에서 이 일치 끝까지 읽은 만큼 센다
        budget.spend(m.end() - prev)
        prev = m.end()
        # 앞 속성 값 안에서 찾은 일치는 이미 읽은 값의 일부다. 건너뛴다
        if m.start() < value_end:
            continue
        if m.group(1).lower() == "href" and not m.group(3):
            continue
        q = m.group(2)
        if q in dead:
            continue
        end = _attr_end(blanked, m.end(), q, budget)
        if end < 0:
            dead.add(q)
            continue
        value_end = end
        budget.spend(end - m.end())
        scripts.append((_unescape(blanked[m.end():end]), None))
    budget.spend(len(blanked) - prev)
    return blanked, scripts


def _calls(js: "_Js") -> list[tuple[str, str]]:
    text = js.text
    # 호출 이름 검색은 본문 전체를 한 번 읽는다
    js.budget.spend(len(text))
    found: list[tuple[str, str]] = []
    for m in _CALL.finditer(text):
        if m.group("api"):
            args = js.args(m.end() - 1, 1)
            if args and args[0][1] > args[0][0]:
                verb = m.group("api").upper()
                found.append(("DELETE" if verb == "DEL" else verb, js.url(*args[0])))
            continue
        args = js.args(m.end() - 1, 2)
        if not args or args[0][1] <= args[0][0]:
            continue
        if m.group("fetch"):
            verb = "GET"
            if len(args) > 1:
                method = js.props(*args[1]).get("method")
                verb = ((js.literal(*method) if method else "") or verb).upper()
            found.append((verb, js.url(*args[0])))
            continue
        if text[args[0][0]] == "{":
            props = js.props(*args[0])
            url = props.get("url")
        else:
            url = args[0]
            props = js.props(*args[1]) if len(args) > 1 else {}
        verb = ""
        for key in ("type", "method"):
            if not verb and key in props:
                verb = js.literal(*props[key]) or ""
        if url:
            found.append(((verb or "GET").upper(), js.url(*url)))
    return [(v, p) for v, p in found if p]


# ---- 진입점 -----------------------------------------------------------------


def _scan_text(rel: str, text: str, suffix: str, limit: int) -> tuple[list[tuple[str, str, str]], list[tuple[str, str]]]:
    """파일 하나에서 (``.mvc`` 주소 목록, REST 호출 목록)을 읽는다. 일이 너무 많으면 ``_TooMuchWork``를 던진다."""
    budget = _Budget(len(text))
    if rel.lower().endswith(".jsp"):
        mvc_text, scripts = _jsp_parts(text, budget)
        # 스크립트 블록은 주석을 지울 때 읽은 결과를 다시 쓴다. 속성 값만 새로 읽는다
        found = [c for piece, js in scripts if _may_call(piece, budget)
                 for c in _calls(js or _Js(piece, budget, jsp=True))]
    else:
        js = _Js(text, budget)
        # 크기 제한보다 큰 JS 파일은 화면 주소에서 뺀다. 묶어 둔 외부 라이브러리일 가능성이 크다
        mvc_text = js.blanked() if len(text) <= limit else ""
        found = _calls(js) if _may_call(text, budget) else []
    mvc = _mvc_urls(mvc_text, suffix, budget) if mvc_text else []
    return mvc, found


def extract_api_map(src_root: Path, rules: ConvertRules, files: Optional[list[str]] = None) -> FrontendApiMap:
    """원본의 JSP와 JS 파일에서 화면 주소, ``.mvc`` 주소 접두사, REST 호출을 모은다.
    ``files``가 None이면 ``src_root``를 직접 순회한다.
    읽지 못한 파일, 너무 큰 파일, 원본 폴더 밖을 가리키는 파일, 읽는 데 일이 너무 많은 파일은 건너뛴다.
    건너뛴 목록은 ``scan_skipped``가 준다.
    ``js_scan_limit_kb``보다 큰 JS 파일은 화면 주소와 접두사에서 빼고 REST 호출만 읽는다."""
    src_root = Path(src_root)
    suffix, limit = _settings(rules)
    prefix = rules.text("frontend.api_prefix", "/api").rstrip("/") or "/api"
    excluded = {e.lower() for e in rules.strs("frontend.route_exclude")}

    urls: dict[str, set[str]] = {}
    votes: Counter[str] = Counter()
    calls: dict[tuple[str, str], str] = {}
    for rel, text, _why in _front_files(src_root, rules, files):
        if text is None:
            continue
        try:
            mvc, found = _scan_text(rel, text, suffix, limit)
        except _TooMuchWork:
            # 일이 너무 많은 파일에서는 아무것도 쓰지 않는다. scan_skipped가 이 파일을 알려 준다
            continue
        except Exception:  # noqa: BLE001 - 예상하지 못한 오류가 난 파일도 건너뛴다. 다른 파일은 계속 읽는다
            continue
        for name, spelled, pfx in mvc:
            # 접두사 투표에는 제외한 화면의 주소도 넣는다
            if pfx:
                votes[pfx] += 1
            route = route_name(name)
            if route and route not in excluded and name.lower() not in excluded:
                urls.setdefault(route, set()).add(spelled)
        for verb, p in found:
            api = _api_path(p, prefix)
            if api:
                calls.setdefault((verb, api), rel)
    mvc_prefix = ""
    if votes:
        mvc_prefix = sorted(votes.items(), key=lambda kv: (-kv[1], -len(kv[0]), kv[0]))[0][0]
    rest = [RestCall(verb=v, path=p, source=src)
            for (v, p), src in sorted(calls.items(), key=lambda kv: (kv[0][1], kv[0][0]))]
    return FrontendApiMap(rest_endpoints=rest, page_routes=sorted(urls), mvc_url_prefix=mvc_prefix,
                          page_urls={r: sorted(u) for r, u in sorted(urls.items())})
