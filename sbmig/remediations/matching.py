"""IVAS가 보고한 g:a와 버전을 Maven이 고른 의존성과 비교할 때 쓰는 함수를 모은 모듈.
Maven은 버전의 한정자를 대소문자 구분 없이 비교하므로 ``2.2.0.release``와 ``2.2.0.RELEASE``는 같은 버전이다.
대소문자와 앞뒤 공백만 무시하고 그 밖의 정규화는 하지 않는다. ``2.2.0``과 ``2.2.0.RELEASE``는 다른 버전이다.

Shared comparison of an IVAS-reported (g:a, version) with a Maven-resolved one. Maven compares version qualifiers
case-insensitively, so case (``str.casefold``) and surrounding whitespace are ignored; nothing else is normalised.
Callers match an exact g:a first and fall back to ``same_ga`` only when no exact g:a exists (Maven's g:a itself is
case-sensitive)."""
from __future__ import annotations


def version_key(version: str | None) -> str:
    """버전 비교용 키를 반환한다. 앞뒤 공백을 빼고 casefold하며, None이나 빈 값은 빈 문자열이다."""
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
