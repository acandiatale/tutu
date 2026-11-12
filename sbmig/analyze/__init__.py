"""프로젝트를 분석해 ProjectProfile을 만드는 패키지.
pom 구조, 소스, 사용 라이브러리를 살펴 프로젝트 유형을 분류한다.
접수 직후 오케스트레이터의 ``step_analyze``가 :func:`analyze_project`를 호출하고, 접수 단계에서도
:func:`detect_build_tool`과 ``aggregator``를 쓴다.
- 주요 함수는 ``analyze_project`` / ``analyze_path``, ``classify``, ``detect_libraries``,
  ``scan_properties``, ``scan_webapp_tree``, ``describe_ant`` / ``synthesize_pom``, ``resolve_jar``다
- 분석은 파일을 읽기만 한다.
  접수 단계에서 호출하는 ``write_aggregator``와 ``synthesize_pom``만 파일을 기록한다

sbmig.analyze — pom graph, source scan, library detection and case classification → ProjectProfile."""
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
