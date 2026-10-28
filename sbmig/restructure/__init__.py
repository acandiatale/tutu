"""재구성(restructure) 패키지의 공개 창구 — 회사 레이아웃으로 디렉토리와 pom 을 바꾸는 단계.
흐름상 위치: 변환(OpenRewrite 1회) 다음, 구조 검토 전의 ``restructure.*`` 단계에서 오케스트레이터가
``Restructurer().apply(ws, profile, cfg, mode=..., params=...)``
를 부른다. 이 단계에는 LLM 이 개입하지 않는다(결정적). 공개 이름: ``Restructurer`` / ``RestructureError`` /
``sanitize_identifier`` / ``sanitize_package``
(restructurer.py), pom 편집기 ``PomEditor`` / ``PomEditError`` /
``ArtifactRef`` / ``property_ref`` (pom_editor.py).
이 목록은 docs/INTERFACES.md 의 계약이므로 이름을 바꾸거나 빼지 않는다.

sbmig.restructure — see docs/INTERFACES.md for the public entry points this package must expose."""
from .pom_editor import ArtifactRef, PomEditError, PomEditor, property_ref
from .restructurer import RestructureError, Restructurer, sanitize_identifier, sanitize_package

__all__ = [
    "ArtifactRef",
    "PomEditError",
    "PomEditor",
    "RestructureError",
    "Restructurer",
    "property_ref",
    "sanitize_identifier",
    "sanitize_package",
]
