"""JS, JSP 파일의 공통 ajax 호출 주소를 REST 주소로 바꾸는 모듈.

Contract: docs/FW-CONVERT.md section 2.5 (the pattern table, scenario A9) and section 4 (``[AJAX_UNMATCHED]``).
Names come from the rules: ``frontend.ajax_endpoint``, ``frontend.service_param``, ``naming.service_suffix``,
``frontend.page_param``, ``frontend.api_prefix``, ``frontend.common_js_prefix``.

How a call is read: every occurrence of the endpoint name must sit inside a quoted string literal (one line, ``'`` or
``"``); that literal is L1. Its text after the endpoint is ``?`` followed by ``name=value&`` items, where only the
service parameter (``X-service``) and the page flag (``=1``) are allowed, and it ends with ``&``, with ``?`` or with
``ServiceName=`` (pattern B: ``+ sn + "-service&" + t +`` follows). Then ``+ t +`` and a literal starting with ``=1``
whose remainder is empty or starts with ``&``. Everything of L1 in front of the endpoint is dropped; an expression in
front of L1 stays. The operator text between the operands (whitespace around ``+``) is reused, so the output keeps the
source's spacing; new literals take the quote of the literal they replace. The query of the result is the page flag
followed by the original rest. A result never ends in ``?`` or ``&``: an empty query is dropped, a trailing ``&`` is
cut unless more concatenation follows (then the literal keeps a trailing ``?`` / ``&`` for it). When the ``=1`` literal
is followed by more concatenation and the result has no query: a leading ``&`` of a following literal becomes ``?``;
an operand that is not a literal gets a ``"?"`` literal in front of it (``+ "?" + qs``; ``?&a=1`` is a valid query).

Files: ``*.js`` and ``*.jsp`` under ``layout.static_dir`` (a JSP expression or EL in the literal in front of the
endpoint goes with the rest of that prefix; for C / D a JSP never falls back to its file name: without a form-field
assignment or a ``serviceName`` variable it is ``[AJAX_UNMATCHED]``). ``AjaxResult.services``: per rewritten file
(path relative to the static folder) the service names its calls name literally (A / E: ``X`` without the suffix,
C / D: the inferred name), sorted, unique; B / F (service from an expression) are not listed. A file over
``frontcopy.scan_limit`` is not read and is an ``errors`` entry. One pass per file: the opening quote of a call is
searched only between the end of the previous call and the endpoint.

A file is all or nothing: any occurrence that does not fit, an endpoint name left anywhere (a comment too) or a C / D
call without an inferable service leaves the file byte-identical and gives one ``QueueItem``. Its ``reason`` never holds
text copied from the file (a line may carry a secret): it says in our own words what was not understood plus the
line and column in the ORIGINAL file (a position in the rewritten text is mapped back, ``_original_pos``); the line
itself goes to ``last_error`` and the start of the file to ``excerpt``, which only reach the run's queue file. Files are read as bytes,
decoded with ``frontcopy.decode_source`` and written back in the same encoding only when that encoding reproduces the
original bytes (CRLF and a BOM survive because nothing translates newlines)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Optional

from sbmig.fwconvert.frontcopy import _write_in_place, decode_source, is_regular, scan_limit
from sbmig.fwconvert.model import Layout, QueueItem
from sbmig.fwconvert.naming import lower_first, strip_suffix
from sbmig.fwconvert.rules import ConvertRules

EXCERPT_CHARS = 2000
LINE_CHARS = 200
_IDENT = re.compile(r"[A-Za-z_$][\w$]*")
_PLUS = re.compile(r"\s*\+\s*")
_SEGMENT = re.compile(r"[\w\-]+")


# ---- JS 조각 읽기 -----------------------------------------------------------


def _string_end(text: str, i: int) -> int:
    """``text[i]``의 따옴표로 시작하는 한 줄 문자열 리터럴의 끝 다음 위치를 반환한다. 아니면 -1이다."""
    if i >= len(text) or text[i] not in "'\"":
        return -1
    q = text[i]
    j = i + 1
    while j < len(text):
        c = text[j]
        if c == "\\":
            j += 2
            continue
        if c == q:
            return j + 1
        if c in "\r\n":
            return -1
        j += 1
    return -1


def _skip_balanced(text: str, i: int) -> int:
    """``text[i]``의 여는 괄호와 짝이 맞는 닫는 괄호 다음 위치를 반환한다. 짝이 없으면 -1이다."""
    pairs = {"(": ")", "[": "]", "{": "}"}
    stack = [pairs[text[i]]]
    j = i + 1
    while j < len(text):
        c = text[j]
        if c in "'\"`":
            end = _string_end(text, j) if c != "`" else text.find("`", j + 1) + 1
            if end <= 0:
                return -1
            j = end
            continue
        if c in pairs:
            stack.append(pairs[c])
        elif c in ")]}":
            if c != stack[-1]:
                return -1
            stack.pop()
            if not stack:
                return j + 1
        j += 1
    return -1


def _expr_end(text: str, i: int) -> int:
    """``i``에서 시작하는 식의 끝 다음 위치를 반환한다. 식이 아니면 -1이다.
    식은 식별자 뒤에 ``.name``, ``[...]``, ``(...)``가 이어진 꼴이다."""
    m = _IDENT.match(text, i)
    if not m:
        return -1
    j = m.end()
    while j < len(text):
        c = text[j]
        if c == "." and _IDENT.match(text, j + 1):
            j = _IDENT.match(text, j + 1).end()
        elif c in "([":
            k = _skip_balanced(text, j)
            if k < 0:
                return -1
            j = k
        else:
            break
    return j


def _plus(text: str, i: int) -> Optional[re.Match[str]]:
    return _PLUS.match(text, i)


# ---- 한 파일 바꾸기 ---------------------------------------------------------


@dataclass
class _Names:
    endpoint: str
    service_param: str
    service_suffix: str
    page_param: str
    api_prefix: str
    common_prefix: str

    @classmethod
    def of(cls, rules: ConvertRules) -> "_Names":
        return cls(rules.text("frontend.ajax_endpoint", "commonAjax.json"),
                   rules.text("frontend.service_param", "ServiceName"),
                   rules.text("naming.service_suffix", "-service"),
                   rules.text("frontend.page_param", "isPage"),
                   rules.text("frontend.api_prefix", "/api").rstrip("/"),
                   rules.text("frontend.common_js_prefix", "common"))


@dataclass
class _Outcome:
    text: str
    count: int
    problem: str = ""               # 비어 있지 않으면 파일을 그대로 둔다. 무엇을 이해하지 못했는지 우리 말로 쓴다
    services: set[str] = field(default_factory=set)     # 바꾼 호출이 글자 그대로 부르는 서비스 이름
    line: str = ""                  # 문제가 된 원본 줄. 이유에는 넣지 않는다. 비밀 값이 들어 있을 수 있다
    line_no: int = 0                # 원본 파일의 줄 번호. 1부터 센다
    col: int = 0                    # 그 줄의 칸 번호. 1부터 센다


def _where(text: str, pos: int) -> tuple[int, int]:
    """위치의 (줄 번호, 칸 번호)를 반환한다. 둘 다 1부터 센다."""
    start = text.rfind("\n", 0, pos) + 1
    return text.count("\n", 0, pos) + 1, pos - start + 1


def _problem(text: str, pos: int, why: str) -> _Outcome:
    """원본을 그대로 두는 결과를 만든다. 이유에는 줄 번호만 쓰고 줄 내용은 ``line``에 따로 둔다."""
    line_no, col = _where(text, pos)
    return _Outcome(text, 0, f"{why} (line {line_no}, column {col})", line=_line_at(text, pos),
                    line_no=line_no, col=col)


def _line_at(text: str, pos: int) -> str:
    start = text.rfind("\n", 0, pos) + 1
    end = text.find("\n", pos)
    line = text[start:end if end >= 0 else len(text)].strip()
    return line if len(line) <= LINE_CHARS else line[:LINE_CHARS] + "..."


def infer_service(text: str, file_name: str, rules: ConvertRules) -> str:
    """ServiceName이 없는 호출에 쓸 서비스 이름을 파일에서 찾는다. 찾지 못하면 빈 문자열을 반환한다.
    - 먼저 ``doc.ServiceName.value = "x-service"``를 찾는다
    - 없으면 ``var serviceName = "x"``를 찾는다
    - 그래도 없으면 JS 파일 이름을 쓴다. 이름이 ``common``으로 시작하는 파일과 JSP 파일은 쓰지 않는다
    찾은 이름 끝의 ``-service``는 뗀다."""
    n = _Names.of(rules)
    sp = re.escape(n.service_param)
    m = re.search(r"\.\s*" + sp + r"\s*\.\s*value\s*=\s*(['\"])([^'\"\r\n]*)\1", text)
    if not m:
        var = re.escape(lower_first(n.service_param))
        m = re.search(r"\b(?:var|let|const)\s+" + var + r"\s*=\s*(['\"])([^'\"\r\n]*)\1", text)
    if m:
        name = strip_suffix(m.group(2).strip(), n.service_suffix)
        return name if _SEGMENT.fullmatch(name) else ""
    base = PurePosixPath(str(file_name).replace("\\", "/")).name
    low = base.lower()
    # JSP는 파일 이름으로 서비스를 짐작하지 않는다. 화면 이름과 서비스 이름은 보통 다르다
    if low.endswith(".jsp"):
        return ""
    stem = base[:-4] if low.endswith(".jsp") else base[:-3] if low.endswith(".js") else base
    stem = stem[:-4] if stem.lower().endswith(".min") else stem
    if not stem or (n.common_prefix and stem.lower().startswith(n.common_prefix.lower())):
        return ""
    stem = strip_suffix(stem, n.service_suffix)
    return stem if _SEGMENT.fullmatch(stem) else ""


def _parse_items(s: str, n: _Names, allow_service: bool) -> Optional[dict[str, str]]:
    """``a=1&b=2`` 꼴의 쿼리 항목을 읽는다. 서비스 이름과 화면 플래그 말고 다른 항목이 있으면 None이다."""
    out: dict[str, str] = {}
    if not s:
        return out
    for item in s.split("&"):
        key, eq, value = item.partition("=")
        if not eq:
            return None
        if allow_service and key == n.service_param and "service" not in out:
            out["service"] = value
        elif key == n.page_param and value == "1" and "page" not in out:
            out["page"] = value
        else:
            return None
    return out


def _rewrite_one(text: str, at: int, n: _Names, inferred: Optional[str],
                 lo: int = 0) -> tuple[Optional[tuple[int, int, str, str]], str]:
    """``at``에 있는 엔드포인트 이름 하나를 바꾼다.
    성공하면 ((시작, 끝, 바꿀 글, 서비스 이름), "")를, 실패하면 (None, 이유)를 반환한다.
    서비스 이름이 식이면(B, F 꼴) 서비스 이름은 빈 문자열이다.
    ``inferred``는 C, D 꼴에 쓸 서비스 이름이다. 아직 모르면 None이다.
    ``lo``는 앞 호출을 바꾼 끝 위치다. 여는 따옴표는 그 뒤에서만 찾는다."""
    # 엔드포인트를 품은 문자열 리터럴의 여는 따옴표를 같은 줄에서 찾는다. 앞 호출 뒤만 보므로 전체가 한 번만 읽힌다
    line_start = max(text.rfind("\n", lo, at) + 1, lo)
    q_pos = max(text.rfind("'", line_start, at), text.rfind('"', line_start, at))
    if q_pos < 0:
        return None, "endpoint outside a string literal"
    l1_end = _string_end(text, q_pos)
    if l1_end < 0 or l1_end <= at + len(n.endpoint):
        return None, "endpoint outside a string literal"
    q = text[q_pos]
    before = text[q_pos + 1:at]
    if before and (before[-1].isalnum() or before[-1] == "_"):
        return None, "endpoint name is part of a longer name"
    after = text[at + len(n.endpoint):l1_end - 1]
    if not after.startswith("?"):
        return None, "endpoint without a query"
    query = after[1:]
    pos = l1_end
    sn_part: Optional[tuple[str, str, str]] = None      # 패턴 B의 (sn 앞 연산자, sn, sn 뒤 연산자)
    if query.endswith(n.service_param + "="):
        # 패턴 B: 서비스 이름이 식으로 이어진다
        head = query[: len(query) - len(n.service_param) - 1]
        if head and not head.endswith("&"):
            return None, "unknown query shape"
        items = _parse_items(head[:-1], n, allow_service=False)
        if items is None:
            return None, "unknown query parameter"
        p1 = _plus(text, pos)
        sn_end = _expr_end(text, p1.end()) if p1 else -1
        if sn_end < 0:
            return None, "service name expression not understood"
        p2 = _plus(text, sn_end)
        if not p2:
            return None, "service name expression not followed by a string"
        l2_start = p2.end()
        l2_end = _string_end(text, l2_start)
        if l2_end < 0:
            return None, "service name expression not followed by a string"
        c2 = text[l2_start + 1:l2_end - 1]
        if not c2.lower().startswith(n.service_suffix.lower()):
            return None, "service name expression without the service suffix"
        rest2 = c2[len(n.service_suffix):]
        if not rest2.startswith("&") or not rest2.endswith("&"):
            return None, "unknown query shape after the service name"
        more = _parse_items(rest2[1:-1], n, allow_service=False)
        if more is None:
            return None, "unknown query parameter"
        items.update(more)
        sn_part = (p1.group(0), text[p1.end():sn_end], p2.group(0))
        pos = l2_end
    else:
        if query and not query.endswith("&"):
            return None, "unknown query shape"
        items = _parse_items(query[:-1], n, allow_service=True)
        if items is None:
            return None, "unknown query parameter"
    # 공통 부분: + t + "=1..."
    pt = _plus(text, pos)
    t_end = _expr_end(text, pt.end()) if pt else -1
    if t_end < 0:
        return None, "transaction expression not understood"
    t = text[pt.end():t_end]
    pl = _plus(text, t_end)
    if not pl:
        return None, "transaction expression not followed by a string"
    last_start = pl.end()
    last_end = _string_end(text, last_start)
    if last_end < 0:
        return None, "transaction expression not followed by a string"
    q_last = text[last_start]
    c_last = text[last_start + 1:last_end - 1]
    if not c_last.startswith("=1"):
        return None, "string after the transaction expression does not start with =1"
    r = c_last[2:]
    if r and not r.startswith("&"):
        return None, "string after the transaction expression does not start with =1"

    if sn_part is None and "service" in items:
        service = strip_suffix(items["service"], n.service_suffix)
        if service == items["service"] or not service:
            return None, "service name without the service suffix"
    elif sn_part is None:
        if inferred is None:
            return None, "infer"
        if not inferred:
            return None, "no service name for a call without ServiceName"
        service = inferred
    else:
        service = ""

    rest = r[1:]
    trailing_amp = r.endswith("&")
    parts = (["{}=1".format(n.page_param)] if "page" in items else []) + [p for p in rest.split("&") if p]
    qs = "&".join(parts)
    cont = _plus(text, last_end)
    continues = bool(cont) and cont.end() < len(text) and text[cont.end()] not in ";,)]}\r\n"
    if continues and trailing_amp:
        tail = "?" + qs + "&" if qs else "?"
    else:
        tail = "?" + qs if qs else ""

    # 새 식을 만든다. 연산자 앞뒤 공백은 원본 것을 그대로 쓴다
    if sn_part is not None:
        op1, sn, op2 = sn_part
        new = f"{q}{n.api_prefix}/{q}{op1}{sn}{op2}{q}/{q}{pt.group(0)}{t}"
    else:
        new = f"{q}{n.api_prefix}/{service}/{q}{pt.group(0)}{t}"
    end = last_end
    if tail:
        new += f"{pl.group(0)}{q_last}{tail}{q_last}"
    elif continues:
        # 쿼리가 없는데 뒤에 문자열이 이어지면 그 문자열의 첫 & 를 ? 로 바꾼다
        nxt = cont.end()
        nxt_end = _string_end(text, nxt)
        if nxt_end > 0 and text[nxt + 1:nxt + 2] == "&":
            new += f"{cont.group(0)}{text[nxt]}?"
            end = nxt + 2
        elif nxt_end < 0:
            # 문자열이 아닌 식이 이어지면 그 앞에 "?"를 넣는다. 식의 값은 보통 "&a=1" 꼴이다
            new += f"{pl.group(0)}{q_last}?{q_last}"
    return (q_pos, end, new, service), ""


def _rewrite(text: str, file_name: str, rules: ConvertRules) -> _Outcome:
    n = _Names.of(rules)
    if not n.endpoint or n.endpoint not in text:
        return _Outcome(text, 0)
    inferred: Optional[str] = None
    services: set[str] = set()
    edits: list[tuple[int, int, int]] = []     # 바꾼 범위(원본 시작, 원본 끝, 새 글 길이)
    out: list[str] = []
    pos = count = 0
    search = 0
    while True:
        at = text.find(n.endpoint, search)
        if at < 0:
            break
        if at < pos:
            search = at + len(n.endpoint)
            continue
        edit, why = _rewrite_one(text, at, n, inferred, pos)
        if why == "infer":
            inferred = infer_service(text, file_name, rules)
            edit, why = _rewrite_one(text, at, n, inferred, pos)
        if edit is None:
            return _problem(text, at, why)
        start, end, new, service = edit
        edits.append((start, end, len(new)))
        if service:
            services.add(service)
        out.append(text[pos:start])
        out.append(new)
        pos = end
        search = end
        count += 1
    out.append(text[pos:])
    new_text = "".join(out)
    left = new_text.find(n.endpoint)
    if left >= 0:
        # 남은 위치를 원본 위치로 되돌린다. 파일은 원본 그대로 두므로 줄 번호도 원본 기준이다
        return _problem(text, _original_pos(left, edits), "endpoint name still present")
    return _Outcome(new_text, count, services=services)


def _original_pos(new_pos: int, edits: list[tuple[int, int, int]]) -> int:
    """바꾼 본문의 위치를 원본 본문의 위치로 바꾼다. 바꾼 범위 안이면 그 범위의 시작이다."""
    delta = 0
    for start, end, new_len in edits:
        new_start = start + delta
        if new_pos < new_start:
            break
        if new_pos < new_start + new_len:
            return start
        delta += new_len - (end - start)
    return new_pos - delta


def rewrite_ajax_text(text: str, file_name: str, rules: ConvertRules) -> tuple[str, int, bool]:
    """JS 본문의 공통 ajax 호출 주소를 REST 주소로 바꾸고 (새 본문, 바꾼 수, 실패 여부)를 반환한다.
    바꾸지 못한 호출이 하나라도 있거나 서비스 이름을 알 수 없으면 원본 본문과 0, True를 반환한다.
    엔드포인트 이름이 없는 본문은 그대로 0, False다.
    ``file_name``은 서비스 이름을 파일 이름에서 정할 때 쓴다."""
    res = _rewrite(text, file_name, rules)
    return res.text, res.count, bool(res.problem)


# ---- 출력 폴더의 JS, JSP 파일 ----------------------------------------------------


@dataclass
class AjaxResult:
    """ajax 주소 변환 결과를 담는 클래스. 경로는 출력 루트 기준이다."""
    files_transformed: list[str] = field(default_factory=list)
    replaced: int = 0
    unmatched_files: list[str] = field(default_factory=list)
    queue: list[QueueItem] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    # 바꾼 파일마다(정적 파일 폴더 기준 경로) 호출이 글자 그대로 부르는 서비스 이름. B, F 꼴의 식은 넣지 않는다
    services: dict[str, list[str]] = field(default_factory=dict)


def transform_ajax(out_root: Path, layout: Layout, rules: ConvertRules) -> AjaxResult:
    """출력 폴더의 정적 파일 폴더에 있는 JS, JSP 파일의 ajax 호출 주소를 바꾼다. 파일은 제자리에서 고친다.
    파일의 인코딩과 줄바꿈은 그대로 둔다. 엔드포인트 이름이 없는 파일은 건드리지 않는다.
    바꾸지 못한 파일은 원래 내용 그대로 두고 ``unmatched_files``와 인수인계 항목에 남긴다.
    크기 제한보다 큰 파일은 읽지 않고 ``errors``에 남긴다."""
    out_root = Path(out_root)
    result = AjaxResult()
    static = out_root / layout.static_dir
    if not static.is_dir():
        return result
    n = _Names.of(rules)
    limit = scan_limit(rules)
    # 이름이 맞으면 일반 파일이 아니어도 목록에 넣는다. 아래에서 오류로 남긴다
    paths = sorted((p for p in static.rglob("*")
                    if p.suffix.lower() in (".js", ".jsp") and not p.is_symlink() and not p.is_dir()),
                   key=lambda p: p.relative_to(static).as_posix())
    for path in paths:
        rel_out = path.relative_to(out_root).as_posix()
        rel_static = path.relative_to(static).as_posix()
        try:
            # 이름 있는 파이프나 장치 파일은 읽으면 멈출 수 있다. 고치지 않고 오류로 남긴다
            if not is_regular(path):
                result.errors.append(f"{rel_out}: not a regular file, not rewritten")
                continue
            if path.stat().st_size > limit:
                result.errors.append(f"{rel_out}: larger than the scan limit of {limit // 1024} KB, not read")
                continue
            raw = path.read_bytes()
        except OSError as e:
            result.errors.append(f"{rel_out}: cannot read ({e.__class__.__name__}: {e})")
            continue
        text, enc = decode_source(raw)
        if n.endpoint not in text:
            continue
        res = _rewrite(text, path.name, rules)
        if res.problem:
            result.unmatched_files.append(rel_out)
            # 이유에는 프로젝트 파일의 글을 넣지 않는다. 줄은 last_error와 excerpt에만 둔다
            result.queue.append(QueueItem(component=f"js_ajax::{rel_static}",
                                          reason=f"[AJAX_UNMATCHED] {res.problem}",
                                          source_path=rel_out, last_error=res.line,
                                          excerpt=text[:EXCERPT_CHARS]))
            continue
        if res.count == 0:
            continue
        try:
            same = text.encode(enc) == raw
            data = res.text.encode(enc)
        except UnicodeEncodeError:
            same = False
        if not same:
            result.errors.append(f"{rel_out}: the {enc} text does not reproduce the file's bytes, not rewritten")
            continue
        try:
            _write_in_place(path, data)
        except OSError as e:
            result.errors.append(f"{rel_out}: cannot write ({e.__class__.__name__}: {e})")
            continue
        result.files_transformed.append(rel_out)
        result.replaced += res.count
        if res.services:
            result.services[rel_static] = sorted(res.services)
    return result
