"""프로젝트의 디렉토리와 pom을 회사 레이아웃으로 바꾸는 재구성 패키지의 공개 진입점.
오케스트레이터가 변환 다음, 구조 검토 전의 ``restructure.*`` 단계에서
``Restructurer().apply(ws, profile, cfg, mode=..., params=...)``를 호출한다. LLM은 사용하지 않는다.
- restructurer.py: ``Restructurer`` / ``RestructureError`` / ``sanitize_identifier`` / ``sanitize_package``
- pom_editor.py: ``PomEditor`` / ``PomEditError`` / ``ArtifactRef`` / ``property_ref``
이 이름들은 docs/INTERFACES.md에 정해진 공개 인터페이스이므로 바꾸거나 빼지 않는다.

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
