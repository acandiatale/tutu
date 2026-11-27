"""IVAS가 보고한 g:a와 버전을 Maven이 고른 의존성과 비교하는 함수를 모은 모듈.
대소문자와 앞뒤 공백만 무시하고 비교한다.

Keys use ``str.casefold``. Callers match an exact g:a first and fall back to ``same_ga`` only when no exact
g:a exists (Maven's g:a itself is case-sensitive).

Maven compares version qualifiers case-insensitively, so ``2.2.0.release`` and ``2.2.0.RELEASE`` are one
version. Nothing else is normalised: ``2.2.0`` and ``2.2.0.RELEASE`` are different versions."""
from __future__ import annotations


def version_key(version: str | None) -> str:
    """버전 비교용 키를 반환한다. 앞뒤 공백을 빼고 casefold한다. None이나 빈 값은 빈 문자열이다."""
    return (version or "").strip().casefold()


def ga_key(ga: str | None) -> str:
    """g:a 비교용 키를 반환한다. 앞뒤 공백을 빼고 casefold한다."""
    return (ga or "").strip().casefold()


def same_version(a: str | None, b: str | None) -> bool:
    """두 버전이 대소문자와 앞뒤 공백만 다르면 True를 반환한다."""
    return version_key(a) == version_key(b)


def same_ga(a: str | None, b: str | None) -> bool:
    """두 g:a가 대소문자와 앞뒤 공백만 다르면 True를 반환한다."""
    return ga_key(a) == ga_key(b)


def finding_key(kind: str | None, cve: str | None, identifier: str | None) -> str:
    """같은 취약점인지 가리는 키를 반환한다. 종류, 취약점 ID, 식별자로 만들고 버전은 넣지 않는다.
    식별자의 대소문자는 무시한다.

    The identity of "the same finding" for counting what was fixed: kind, finding id and the case-folded identifier,
    no version. A finding a complete final scan still reports for the library at any version (or in another spelling
    of the version) is not fixed. `graph.fill_counts` and the Korean report both use this key."""
    return f"{kind or ''}|{cve or ''}|{ga_key(identifier)}"
