"""Deterministic handlers (pom version floors, relocations, jakarta swaps)."""
from .base import Handler, HandlerRegistry, all_poms, nearest_pom, poms_for_cluster, version_lt
from .pom_handlers import (CompilerPluginFloor, DependencyRelocation, JakartaLibraryFloor, JavaxServletApiToJakarta,
                           JaxbRuntimeAdd, LombokVersionFloor, MapstructVersionFloor, MissingDependencyVersion,
                           MysqlConnectorRelocation, PomHandler, QuerydslJakartaClassifier, Relocation,
                           RelocationHandler, RemoveClassicLoaderImplementation, ResourcesBinaryFiltering,
                           StripPomRepositories, SurefirePluginFloor,
                           TestCompileSkip, default_handlers, jakarta_libraries_below, known_versions,
                           relocation_table, version_for)
from .source_handlers import (JakartaNamespaceSweep, RenameResult, SpringDataCrudRepository, jakarta_packages,
                              rename_javax)
from .jdk_compat import JdkBase64Shim
from .spring7_sources import (Boot4TypeRelocationSweep, SpringDataSpecificationUnrestricted,
                              SpringSecurityPathPatternMatcher)

__all__ = [
    "Handler", "HandlerRegistry", "PomHandler", "RelocationHandler", "Relocation", "default_handlers",
    "all_poms", "nearest_pom", "poms_for_cluster", "version_lt", "known_versions", "relocation_table",
    "version_for", "CompilerPluginFloor", "DependencyRelocation", "JavaxServletApiToJakarta", "JaxbRuntimeAdd",
    "LombokVersionFloor", "MapstructVersionFloor", "MissingDependencyVersion", "MysqlConnectorRelocation",
    "QuerydslJakartaClassifier", "RemoveClassicLoaderImplementation", "ResourcesBinaryFiltering", "StripPomRepositories", "SurefirePluginFloor",
    "TestCompileSkip", "JakartaNamespaceSweep", "SpringDataCrudRepository", "RenameResult", "jakarta_packages",
    "rename_javax", "JakartaLibraryFloor", "jakarta_libraries_below",
    "JdkBase64Shim", "Boot4TypeRelocationSweep", "SpringSecurityPathPatternMatcher", "SpringDataSpecificationUnrestricted",
]
