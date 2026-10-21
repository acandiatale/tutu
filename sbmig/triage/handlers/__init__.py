"""Deterministic handlers (pom version floors, relocations, jakarta swaps)."""
from .base import Handler, HandlerRegistry, all_poms, nearest_pom, poms_for_cluster, version_lt
from .pom_handlers import (CompilerPluginFloor, DependencyRelocation, JavaxServletApiToJakarta, JaxbRuntimeAdd,
                           LombokVersionFloor, MapstructVersionFloor, MissingDependencyVersion,
                           MysqlConnectorRelocation, PomHandler, QuerydslJakartaClassifier, Relocation,
                           RelocationHandler, RemoveClassicLoaderImplementation, ResourcesBinaryFiltering,
                           StripPomRepositories, SurefirePluginFloor,
                           TestCompileSkip, default_handlers, known_versions, relocation_table, version_for)

__all__ = [
    "Handler", "HandlerRegistry", "PomHandler", "RelocationHandler", "Relocation", "default_handlers",
    "all_poms", "nearest_pom", "poms_for_cluster", "version_lt", "known_versions", "relocation_table",
    "version_for", "CompilerPluginFloor", "DependencyRelocation", "JavaxServletApiToJakarta", "JaxbRuntimeAdd",
    "LombokVersionFloor", "MapstructVersionFloor", "MissingDependencyVersion", "MysqlConnectorRelocation",
    "QuerydslJakartaClassifier", "RemoveClassicLoaderImplementation", "ResourcesBinaryFiltering", "StripPomRepositories", "SurefirePluginFloor",
    "TestCompileSkip",
]
