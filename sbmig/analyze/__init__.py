"""sbmig.analyze — pom graph, source scan, library detection and case classification → ProjectProfile."""
from .analyzer import PathView, analyze_path, analyze_project
from .build_tool import BuildToolDetection, detect_build_tool
from .classifier import (classify, classify_single, deployable_services, is_library_root, library_roots,
                         library_services)
from .edges import (SystemDep, detect_encoding, normalize_java_version, resolve_system_path, system_dependencies)
from .libraries import LIBRARY_NAMES, LIBRARY_RULES, detect_libraries
from .pom_parser import module_order, parse_poms
from .properties import ExternalizedRules, load_rules, scan_properties
from .source_scanner import ScanResult, scan_source_stats, scan_sources
from .webapp_inventory import guess_jar, jar_names, scan_webapp_tree

__all__ = [
    "analyze_project", "analyze_path", "PathView", "BuildToolDetection", "detect_build_tool", "classify", "classify_single", "detect_libraries",
    "LIBRARY_NAMES", "LIBRARY_RULES", "module_order", "parse_poms", "scan_sources", "scan_source_stats",
    "ScanResult", "is_library_root", "library_services", "deployable_services", "library_roots", "SystemDep",
    "detect_encoding", "normalize_java_version", "resolve_system_path", "system_dependencies",
    "scan_properties", "load_rules", "ExternalizedRules", "scan_webapp_tree", "jar_names", "guess_jar",
]
