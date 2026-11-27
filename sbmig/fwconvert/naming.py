"""생성할 프로젝트의 이름 규칙을 모아 둔 파일. 클래스 이름, 경로용 이름, 메서드 이름을 만든다.

Contract: docs/FW-CONVERT.md section 3.2. Pure string functions, shared by every module of this package so that two
generators never spell the same resource differently."""
from __future__ import annotations

import re

JAVA_KEYWORDS = frozenset("""abstract assert boolean break byte case catch char class const continue default do double
else enum extends final finally float for goto if implements import instanceof int interface long native new package
private protected public return short static strictfp super switch synchronized this throw throws transient try void
volatile while true false null var record yield""".split())

_SPLIT = re.compile(r"[-_\s.]+")
_CAMEL_EDGE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def pascal(name: str) -> str:
    """``-``와 ``_``로 나눈 조각마다 첫 글자만 대문자로 바꿔 붙인다. 나머지 글자는 그대로 둔다.
    예: ``order-item``은 ``OrderItem``, ``itemLOV``는 ``ItemLOV``."""
    parts = [p for p in _SPLIT.split(str(name or "").strip()) if p]
    return "".join(p[:1].upper() + p[1:] for p in parts)


def camel(name: str) -> str:
    """``pascal``과 같지만 첫 글자는 소문자다. 예: ``order-item``은 ``orderItem``."""
    return lower_first(pascal(name))


def lower_first(name: str) -> str:
    """첫 글자만 소문자로 바꾼다."""
    s = str(name or "")
    return s[:1].lower() + s[1:]


def kebab(name: str) -> str:
    """소문자와 ``-``로 된 이름으로 바꾼다. 예: ``OrderItem``과 ``order_item``은 ``order-item``."""
    parts: list[str] = []
    for p in _SPLIT.split(str(name or "").strip()):
        parts.extend(x for x in _CAMEL_EDGE.split(p) if x)
    return "-".join(x.lower() for x in parts)


def last_token(ident: str) -> str:
    """``.``이 있으면 마지막 조각만 반환한다. 예: ``order.getList``는 ``getList``."""
    s = str(ident or "").strip()
    return s.rsplit(".", 1)[-1] if "." in s else s


def strip_suffix(name: str, suffix: str) -> str:
    """이름 끝에 ``suffix``가 있으면 뗀다. 대소문자는 가리지 않는다."""
    s = str(name or "")
    return s[: len(s) - len(suffix)] if suffix and s.lower().endswith(suffix.lower()) else s


def x_name(resource: str) -> str:
    """리소스 이름으로 클래스 이름의 앞부분 ``{X}``를 만든다. 자바 식별자에 쓸 수 없는 글자는 뺀다.
    숫자로 시작하면 앞에 ``X``를 붙인다."""
    s = re.sub(r"[^A-Za-z0-9_$]", "", pascal(resource))
    return ("X" + s) if s[:1].isdigit() else s


def java_safe(name: str) -> str:
    """자바 메서드 이름으로 쓸 수 있게 바꾼다. 예약어면 끝에 ``_``를 붙인다.
    쓸 수 없는 글자는 ``_``로 바꾸고, 숫자로 시작하면 앞에 ``_``를 붙인다."""
    s = re.sub(r"[^A-Za-z0-9_$]", "_", str(name or "").strip())
    if not s:
        return "_"
    if s[0].isdigit():
        s = "_" + s
    return s + "_" if s in JAVA_KEYWORDS else s


def method_name(ident: str) -> str:
    """statement id나 activity 이름으로 메서드 이름을 만든다. 마지막 조각을 쓰고 첫 글자는 소문자다."""
    return java_safe(lower_first(camel_keep(last_token(ident))))


def camel_keep(name: str) -> str:
    """``-``나 ``_``가 있을 때만 조각을 이어 붙인다. 없으면 이름을 그대로 둔다(``getList``는 ``getList``)."""
    s = str(name or "").strip()
    return camel(s) if re.search(r"[-_\s]", s) else s


def package_path(package: str) -> str:
    """패키지 이름을 디렉터리 경로로 바꾼다. 예: ``com.example.shop``은 ``com/example/shop``."""
    return str(package or "").replace(".", "/")
