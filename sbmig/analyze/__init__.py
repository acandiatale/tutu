"""프로젝트의 pom, 소스, 사용 라이브러리를 살펴 프로젝트 유형을 분류하는 패키지.

The orchestrator's ``step_analyze`` calls :func:`analyze_project` right after intake; intake itself uses
:func:`detect_build_tool` and ``aggregator``. Analysis only reads files: ``write_aggregator`` and
``synthesize_pom`` (both called at intake) are the only writers.
"""
from .analyzer import PathView, analyze_path, analyze_project
from .adf import ADF_FLAG, TRACES_FLAG, AdfInventory, adf_followup, adf_sizing_lines, detect_adf, traces_followup
from .ant import AntProject, describe_ant, synthesize_pom
from .build_tool import BuildToolDetection, detect_build_tool, find_ant_build_file
from .classifier import (classify, classify_single, deployable_services, is_library_root, library_roots,
                         library_services)
from .edges import (SystemDep, detect_encoding, normalize_java_version, resolve_system_path, system_dependencies)
from .jar_coords import Sha1Lookup, jar_packages, mark_used, resolve_jar, stem_and_version
from .libraries import LIBRARY_NAMES, LIBRARY_RULES, detect_libraries
from .pom_parser import module_order, parse_poms
from .properties import ExternalizedRules, load_rules, scan_properties
from .source_scanner import ScanResult, scan_source_stats, scan_sources
from .webapp_inventory import guess_jar, jar_names, scan_webapp_tree
from .webxml import parse_web_xml

__all__ = [
    "analyze_project", "analyze_path", "PathView", "BuildToolDetection", "detect_build_tool", "classify", "classify_single", "detect_libraries",
    "LIBRARY_NAMES", "LIBRARY_RULES", "module_order", "parse_poms", "scan_sources", "scan_source_stats",
    "ScanResult", "is_library_root", "library_services", "deployable_services", "library_roots", "SystemDep",
    "detect_encoding", "normalize_java_version", "resolve_system_path", "system_dependencies",
    "scan_properties", "load_rules", "ExternalizedRules", "scan_webapp_tree", "jar_names", "guess_jar",
    "parse_web_xml", "AntProject", "describe_ant", "synthesize_pom", "find_ant_build_file", "resolve_jar",
    "jar_packages", "mark_used", "stem_and_version", "Sha1Lookup",
]
