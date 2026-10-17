"""sbmig.analyze — pom graph, source scan, library detection and case classification → ProjectProfile."""
from .analyzer import PathView, analyze_path, analyze_project
from .classifier import classify, classify_single
from .libraries import LIBRARY_NAMES, LIBRARY_RULES, detect_libraries
from .pom_parser import module_order, parse_poms
from .source_scanner import ScanResult, scan_source_stats, scan_sources

__all__ = [
    "analyze_project", "analyze_path", "PathView", "classify", "classify_single", "detect_libraries",
    "LIBRARY_NAMES", "LIBRARY_RULES", "module_order", "parse_poms", "scan_sources", "scan_source_stats",
    "ScanResult",
]
