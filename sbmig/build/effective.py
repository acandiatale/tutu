"""as-is 부모 체인이 관리하던 의존성 버전(effective dependencyManagement)을 저장하고 읽는 모듈.
재구성이 as-is 부모를 회사 부모로 바꾸면 이전 체인이 관리하던 버전이 모두 사라지므로, 접수 때 바꾸기 전
트리에서 ``mvn help:effective-pom``을 실행해 ``logs/effective-depmgmt.json``에 저장해 둔다.
- 인수인계 섹션의 as-is 버전에 쓴다(``load_effective`` -> ``build_handover``)
- 재구성 뒤 저장본(``depmgmt-after-restructure.json``)은 ``layout.drop_managed_versions``가 새 부모가
  관리하는 명시 버전을 지울 때 쓴다
- 진입점: ``capture_effective_depmgmt``, ``load_effective``, ``parse_effective_pom``, ``effective_version``
- 예외를 던지지 않는다. as-is pom을 해석할 수 없으면 None을 반환하고 실행은 계속한다

Managed versions of the as-is parent chain (catalog E6).

Swapping an as-is parent (Boot starter parent, an old in-house parent …) for the company parent drops every version
the old chain managed. `capture_effective_depmgmt` runs `mvn help:effective-pom` on the **as-is** project once at
intake and stores the versions in `<run_dir>/logs/effective-depmgmt.json` (v2.1: no effective/ directory; the
effective pom xml itself is deleted once parsed, the Maven log stays):

    {"managed": {"g:a": "v"}, "direct": {"g:a": "v"}, "plugins": {"g:a": "v"},
     "properties": {...}, "parent": "g:a:v", "modules": ["g:a", ...],
     "by_module": {"g:a": {"managed": {...}, "direct": {...}, "parent": "g:a:v"}}}

so the `carry-over-managed-version` handler can pin them explicitly when the restructured build reports
`'dependencies.dependency.version' for g:a:jar is missing`. Never raises; None when the as-is pom does not resolve.


"""
from __future__ import annotations

import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any, Optional

from lxml import etree

from sbmig.core.config import Config
from sbmig.core.workspace import Workspace, logs_dir

from .mvn import base_command, mvn_env, run_logged
from .runner import negative_cache_hits

log = logging.getLogger("sbmig.build.effective")

HELP_PLUGIN = "org.apache.maven.plugins:maven-help-plugin:3.5.1"
EFFECTIVE_TIMEOUT_S = 10 * 60
DEPMGMT_FILE = "depmgmt.json"
# as-is 저장본(depmgmt.json)은 인수인계에 쓰는 as-is 버전이므로 덮어쓰지 않도록 재구성 뒤 저장본은 다른 파일에 둔다
AFTER_RESTRUCTURE_FILE = "depmgmt-after-restructure.json"   # round 9: chain of the restructured tree
DEFAULT_PLUGIN_GROUP = "org.apache.maven.plugins"


def _empty() -> dict[str, Any]:
    return {"managed": {}, "direct": {}, "plugins": {}, "properties": {}, "parent": None, "modules": [],
            "by_module": {}}


def _local(tag: Any) -> str:
    return etree.QName(tag).localname if isinstance(tag, str) else ""


def _child(el: Any, name: str) -> Any:
    for c in el:
        if _local(c.tag) == name:
            return c
    return None


def _children(el: Any, name: str) -> list[Any]:
    return [c for c in el if _local(c.tag) == name] if el is not None else []


def _text(el: Any, name: str) -> Optional[str]:
    c = _child(el, name) if el is not None else None
    if c is None or c.text is None:
        return None
    t = c.text.strip()
    return t or None


def _path(el: Any, *names: str) -> Any:
    cur = el
    for n in names:
        if cur is None:
            return None
        cur = _child(cur, n)
    return cur


def _deps_into(container: Any, out: dict[str, str]) -> None:
    for d in _children(container, "dependency"):
        g, a, v = _text(d, "groupId"), _text(d, "artifactId"), _text(d, "version")
        if g and a and v and "${" not in v:
            out.setdefault(f"{g}:{a}", v)


def _plugins_into(container: Any, out: dict[str, str]) -> None:
    for p in _children(container, "plugin"):
        g, a, v = _text(p, "groupId") or DEFAULT_PLUGIN_GROUP, _text(p, "artifactId"), _text(p, "version")
        if a and v and "${" not in v:
            out.setdefault(f"{g}:{a}", v)


def parse_effective_pom(text: str | bytes) -> dict[str, Any]:
    """effective pom 하나를 depmgmt dict로 파싱한다.
    프로젝트가 여러 개이면 먼저 나온 값이 우선하도록 합치고, 모듈별 값은 ``by_module``에 둔다.
    ``${…}``가 남은 버전은 버린다.

    Parse one effective pom (single `<project>` or `<projects>` of a reactor) into the depmgmt dict.
    Several projects are merged, first wins (the reactor root comes first)."""
    data = _empty()
    raw = text.encode("utf-8") if isinstance(text, str) else text
    if b"<project" not in raw:
        return data
    parser = etree.XMLParser(recover=True, remove_comments=True, resolve_entities=False, no_network=True,
                             huge_tree=True)
    try:
        root = etree.fromstring(raw, parser)
    except etree.XMLSyntaxError:
        root = None
    if root is None:
        return data
    projects = [root] if _local(root.tag) == "project" else _children(root, "project")
    for pr in projects:
        _deps_into(_path(pr, "dependencyManagement", "dependencies"), data["managed"])
        _deps_into(_child(pr, "dependencies"), data["direct"])
        _plugins_into(_path(pr, "build", "pluginManagement", "plugins"), data["plugins"])
        _plugins_into(_path(pr, "build", "plugins"), data["plugins"])
        props = _child(pr, "properties")
        if props is not None:
            for p in props:
                k = _local(p.tag)
                if k and p.text is not None:
                    data["properties"].setdefault(k, p.text.strip())
        parent = _child(pr, "parent")
        if parent is not None and data["parent"] is None:
            g, a, v = _text(parent, "groupId"), _text(parent, "artifactId"), _text(parent, "version")
            if g and a:
                data["parent"] = f"{g}:{a}" + (f":{v}" if v else "")
        g = _text(pr, "groupId") or (_text(parent, "groupId") if parent is not None else None)
        a = _text(pr, "artifactId")
        if g and a and f"{g}:{a}" not in data["modules"]:
            data["modules"].append(f"{g}:{a}")
            own: dict[str, Any] = {"managed": {}, "direct": {}, "parent": None}
            _deps_into(_path(pr, "dependencyManagement", "dependencies"), own["managed"])
            _deps_into(_child(pr, "dependencies"), own["direct"])
            if parent is not None and _text(parent, "artifactId"):
                own["parent"] = ":".join(x for x in (_text(parent, "groupId"), _text(parent, "artifactId"),
                                                     _text(parent, "version")) if x)
            data["by_module"][f"{g}:{a}"] = own
    return data


def _effective_dir(ws: Workspace) -> Path:
    return logs_dir(ws)


def _effective_path(ws: Workspace, name: str) -> Path:
    """저장 파일 경로 ``<run_dir>/logs/effective-<name>``을 반환한다.

    `<run_dir>/logs/effective-<name>` (depmgmt.json → effective-depmgmt.json)."""
    return _effective_dir(ws) / f"effective-{name}"


def capture_effective_depmgmt(ws: Workspace, cfg: Config, module: Optional[str] = None,
                              name: str = DEPMGMT_FILE) -> Optional[Path]:
    """지금 트리에서 ``help:effective-pom``을 실행해 결과를 ``<name>`` 파일에 쓰고 경로를 반환한다.
    실패하면 None을 반환하고 로그와 저널에 남긴다.
    기본 파일 depmgmt.json은 접수 때의 as-is 저장본이다. 파일이 이미 있으면 기존 값이 우선하도록 합친다.

    Run `help:effective-pom` (whole reactor, or `-pl module`) on the current tree and write `<name>` (default
    depmgmt.json = the as-is capture at intake; round 9: `AFTER_RESTRUCTURE_FILE` for the restructured chain).
    Returns its path; None on failure (logged + journaled). Merges into an existing `<name>` (first wins)."""
    try:
        out_dir = _effective_dir(ws)
        out_dir.mkdir(parents=True, exist_ok=True)
        stem = "".join(c if c.isalnum() or c in "-_." else "_" for c in (module or "root").strip("/")) or "root"
        if name != DEPMGMT_FILE:                    # keep the as-is effective pom xml / log next to the new one
            stem = f"{Path(name).stem}-{stem}"
        xml_path = out_dir / f"effective-pom-{stem}.xml"
        xml_path.unlink(missing_ok=True)
        cmd = base_command(cfg)
        if module:
            cmd += ["-pl", module]
        cmd += [f"{HELP_PLUGIN}:effective-pom", f"-Doutput={xml_path}", "-q"]
        log_path = ws.new_log_path(f"effective-pom-{stem}")
        pr = run_logged(cmd, cwd=ws.root, env=mvn_env(cfg), log_path=log_path, timeout_s=EFFECTIVE_TIMEOUT_S)
        if pr.exit_code != 0 and not xml_path.is_file() and getattr(cfg.build, "retry_negative_cache", True) \
                and not cfg.build.offline and not any(a in ("-U", "--update-snapshots", "-o", "--offline") for a in cmd):
            # 모델 단계의 해석 실패는 남아 있는 `*.lastUpdated` 표시 때문일 수 있으므로 -U로 한 번 다시 시도한다
            hits = negative_cache_hits(pr.text)
            if hits:
                log.info("effective-pom: cached / model-level resolution failure (%s) → retrying once with -U", ", ".join(hits[:3]))
                ws.journal({"event": "effective_pom_force_update", "module": module, "artifacts": hits, "first_log": str(log_path)})
                log_path = ws.new_log_path(f"effective-pom-{stem}-U")
                pr = run_logged([*cmd, "-U"], cwd=ws.root, env=mvn_env(cfg), log_path=log_path, timeout_s=EFFECTIVE_TIMEOUT_S)
        if pr.exit_code != 0 or not xml_path.is_file():
            why = pr.error or ("timed out" if pr.timed_out else _last_error(pr.text) or f"exit {pr.exit_code}")
            # 부모 pom을 저장소에서 받지 못했으면 한 줄로 남기며, 실행은 계속하고 버전을 이어받는 처리만 빠진다
            from .log_parser import parent_coordinates_in
            parents = parent_coordinates_in(pr.text or "")
            if parents:
                why = f"the parent {parents[0].coordinates} is not in the repository"
            log.warning("effective-pom of the as-is project failed (%s): %s — managed versions of the old parent "
                        "cannot be carried over (see %s)", module or "root", why, log_path)
            ws.journal({"event": "effective_pom", "ok": False, "module": module, "file": name, "reason": why,
                        "log": str(log_path)})
            return None
        data = parse_effective_pom(xml_path.read_bytes())
        xml_path.unlink(missing_ok=True)            # parsed: the json below holds what the run needs
        target = _effective_path(ws, name)
        if target.is_file():
            try:
                prev = json.loads(target.read_text(encoding="utf-8"))
                data = _merge(prev, data)
            except (OSError, ValueError):
                pass
        target.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        ws.journal({"event": "effective_pom", "ok": True, "module": module, "file": name,
                    "managed": len(data["managed"]),
                    "direct": len(data["direct"]), "parent": data["parent"], "path": str(target)})
        log.info("effective pom: %d managed / %d direct versions captured (parent %s)", len(data["managed"]),
                 len(data["direct"]), data["parent"])
        return target
    except Exception as exc:
        log.warning("effective-pom capture failed: %s: %s", type(exc).__name__, exc)
        return None


def _merge(first: dict[str, Any], second: dict[str, Any]) -> dict[str, Any]:
    """두 저장본을 합친다. 이미 있던 ``first``의 값이 우선한다."""
    out = _empty()
    for key in ("managed", "direct", "plugins", "properties"):
        merged = dict(second.get(key) or {})
        merged.update(first.get(key) or {})
        out[key] = merged
    out["parent"] = first.get("parent") or second.get("parent")
    out["modules"] = list(dict.fromkeys([*(first.get("modules") or []), *(second.get("modules") or [])]))
    out["by_module"] = {**(second.get("by_module") or {}), **(first.get("by_module") or {})}
    return out


def _last_error(text: str) -> str:
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.startswith("[ERROR]") and len(ln.strip()) > 8]
    return lines[0][len("[ERROR]"):].strip()[:300] if lines else ""


def load_effective(ws: Workspace, name: str = DEPMGMT_FILE) -> dict[str, Any]:
    """저장된 depmgmt dict를 읽어 반환한다. 파일이 없거나 읽지 못하면 빈 형태의 dict를 반환한다.

    The captured depmgmt dict (`<run_dir>/logs/effective-<name>`), or an empty one
    (`{"managed": {}, "direct": {}, …, "parent": None}`)."""
    p = _effective_path(ws, name)
    if not p.is_file():
        return _empty()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("cannot read %s: %s", p, exc)
        return _empty()
    base = _empty()
    base.update({k: v for k, v in data.items() if v is not None})
    return base


def effective_pom_provider(ws: Workspace, cfg: Config, module: Optional[str] = None,
                           name: str = AFTER_RESTRUCTURE_FILE) -> Callable[[], Optional[dict[str, Any]]]:
    """재구성기의 ``params["effective_pom"]``에 넣을 인자 없는 함수를 만든다.
    호출할 때마다 이전 저장본을 지우고 지금 트리를 다시 읽어 dict를 반환한다.
    Maven이 실패하면 None을 반환한다. 오케스트레이터는 같은 일을 하는 ``_effective_pom_after``를 쓴다.

    Round 9: the zero-arg `params["effective_pom"]` callable of the restructurer. Each call drops a previous
    `<name>` (a stale capture must not win the first-wins merge), captures the effective pom of the tree as it is
    *now* (after the restructure's pom edits) and returns the loaded dict — None when Maven fails / is unavailable."""
    def _provide() -> Optional[dict[str, Any]]:
        try:
            _effective_path(ws, name).unlink(missing_ok=True)
        except OSError:
            pass
        if capture_effective_depmgmt(ws, cfg, module, name=name) is None:
            return None
        return load_effective(ws, name)
    return _provide


def effective_version(ws: Workspace, key: str, module: Optional[str] = None,
                      data: Optional[dict[str, Any]] = None) -> Optional[str]:
    """as-is effective pom이 관리하던 g:a의 버전을 반환한다. 관리 버전이 없으면 직접 선언된 버전을 반환한다.
    MSA 서비스마다 부모가 다를 수 있으므로 ``module``을 주면 그 모듈의 값을 먼저 본다.

    Version of g:a managed (else declared) by the as-is effective pom; `module` (artifactId or g:a of the
    declaring module) is preferred over the merged (first-wins) view — MSA services may have different parents."""
    data = data if data is not None else load_effective(ws)
    if module:
        for mkey, own in (data.get("by_module") or {}).items():
            if mkey == module or mkey.split(":")[-1] == module:
                v = (own.get("managed") or {}).get(key) or (own.get("direct") or {}).get(key)
                if v:
                    return v
    return (data.get("managed") or {}).get(key) or (data.get("direct") or {}).get(key)
