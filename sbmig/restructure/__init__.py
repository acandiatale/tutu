"""sbmig.restructure — see docs/INTERFACES.md for the public entry points this package must expose."""
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
