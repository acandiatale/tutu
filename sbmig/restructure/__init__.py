"""프로젝트의 디렉토리와 pom을 회사 레이아웃으로 바꾸는 재구성 패키지. LLM은 쓰지 않는다.

The orchestrator calls `Restructurer().apply(...)` in the `restructure.*` steps, after the transform and
before the structure review. These names are the public interface and are never renamed or dropped:
restructurer.py `Restructurer`, `RestructureError`, `sanitize_identifier`, `sanitize_package`; pom_editor.py
`PomEditor`, `PomEditError`, `ArtifactRef`, `property_ref`.

The public entry points are fixed in docs/INTERFACES.md."""
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
