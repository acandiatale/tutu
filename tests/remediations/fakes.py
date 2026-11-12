"""Shared test doubles for the remediation feature (no IVAS, Nexus, Maven or network).

The intake helpers are tiny *real* implementations over tmp dirs (deterministic ZIP), so graph tests exercise real files.
"""
from __future__ import annotations

import difflib
import hashlib
import io
import json
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from sbmig.core.config import RemediationConfig
from sbmig.remediations.interfaces import Components, Page
from sbmig.remediations.models import (
    D_AUTO,
    ApplyResult,
    BoundaryFinding,
    BuildOutcome,
    DeliveryInfo,
    Finding,
    IntakeResult,
    Inventory,
    InventoryItem,
    IvasUploadUncertain,
    PackagedLib,
    Proposal,
    RemediationState,
    ResolvedModel,
    RoundPlan,
    RunPaths,
    ScanResult,
    TextEdit,
    UploadReceipt,
    canonical_json,
    sha256_text,
)

POLICY_HASH = "policy-hash-1"
ZIP_DATE = (1980, 1, 1, 0, 0, 0)


def make_cfg(**remediation: Any) -> SimpleNamespace:
    """A minimal cfg object: only `cfg.remediation` is read by the graph."""
    return SimpleNamespace(remediation=RemediationConfig(**remediation))


def deterministic_zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in sorted(files):
            info = zipfile.ZipInfo(name, date_time=ZIP_DATE)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, files[name])
    return buf.getvalue()


def file_sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------- intake (real, tiny)
class FakeIntakeFns:
    """Real tiny `manifest_paths` / `tree_hash` / `copy_tree` / `freeze_delivery`; scripted `check_boundary`."""

    def __init__(self, boundary: list[BoundaryFinding] | None = None,
                 after_freeze: Callable[[Path], None] | None = None):
        self.boundary = list(boundary or [])
        self.after_freeze = after_freeze
        self.calls: list[str] = []

    def manifest_paths(self, manifest: Path) -> list[str]:
        data = json.loads(Path(manifest).read_text(encoding="utf-8"))
        entries = (data.get("entries") or data.get("files") or []) if isinstance(data, dict) else data
        return sorted(e["path"] for e in entries)

    def tree_hash(self, tree: Path, paths: list[str]) -> str:
        rows = [[p, file_sha(tree / p) if (tree / p).is_file() else ""] for p in sorted(paths)]
        return sha256_text(canonical_json(rows))

    def check_boundary(self, project: Path, cfg: Any, allowed_hosts: list[str]) -> list[BoundaryFinding]:
        self.calls.append("check_boundary")
        return list(self.boundary)

    def copy_tree(self, src: Path, dst: Path, paths: list[str]) -> None:
        self.calls.append("copy_tree")
        dst.mkdir(parents=True, exist_ok=True)
        for p in paths:
            s = src / p
            if s.is_file():
                d = dst / p
                d.parent.mkdir(parents=True, exist_ok=True)
                d.write_bytes(s.read_bytes())

    def freeze_delivery(self, tree: Path, out_zip: Path, round: int, paths: list[str]) -> DeliveryInfo:
        self.calls.append("freeze_delivery")
        files = {p: (tree / p).read_bytes() for p in paths if (tree / p).is_file()}
        out_zip.parent.mkdir(parents=True, exist_ok=True)
        out_zip.write_bytes(deterministic_zip(files))
        info = DeliveryInfo(path=f"{out_zip.parent.name}/{out_zip.name}", sha256=file_sha(out_zip),
                            size=out_zip.stat().st_size, files=len(files), tree_hash=self.tree_hash(tree, paths),
                            round=round)
        if self.after_freeze is not None:
            self.after_freeze(out_zip)
        return info


# ---------------------------------------------------------------------------- IVAS
class FakeIvas:
    """An `IvasClient`: scripted receipts / statuses / pages; counts uploads; can raise `IvasUploadUncertain`."""

    def __init__(self, statuses: list[str] | None = None, pages: dict[str, list[Page]] | None = None,
                 summary: dict[str, int] | None = None, uncertain: bool = False):
        self.statuses = list(statuses or ["COMPLETE"])
        self.pages = dict(pages or {})
        self._summary = dict(summary or {})
        self.uncertain = uncertain
        self.uploads = 0

    def upload(self, zip_path: Path, project_name: str) -> UploadReceipt:
        self.uploads += 1
        if self.uncertain:
            raise IvasUploadUncertain("upload timed out")
        return UploadReceipt(project_id=f"prj-{self.uploads}", analysis_id=f"anl-{self.uploads}",
                             zip_sha256=file_sha(zip_path), project_name=project_name)

    def status(self, project_id: str, analysis_id: str) -> str:
        return self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]

    def summary(self, project_id: str, analysis_id: str) -> dict[str, int]:
        return dict(self._summary)

    def page(self, project_id: str, analysis_id: str, kind: str, page: int, size: int,
             component_id: str | None = None) -> Page:
        pages = self.pages.get(kind) or []
        return pages[page] if page < len(pages) else Page(content=[], total=0)


def finding(ga: str = "example:lib", version: str = "1.0", cve: str = "CVE-2026-0001") -> Finding:
    return Finding(id=cve, kind="library", identifier=ga, version=version)


def scan_result(*findings: Finding, complete: bool = True, reasons: list[str] | None = None) -> ScanResult:
    """A scripted scan result; `label` / `zip_sha256` / `receipt` are filled in by `FakeScanner`."""
    return ScanResult(label="", zip_sha256="", complete=complete, findings=list(findings),
                      incomplete_reasons=list(reasons or []))


UNCERTAIN = "uncertain"     # scripted item: the upload raises IvasUploadUncertain
STOP = "stop"               # scripted item: upload accepted, then the stop arrives during polling


@dataclass
class ScanCall:
    label: str
    zip_sha256: str
    receipt: UploadReceipt | None
    uploaded: bool


class FakeScanner:
    """A `Scanner`: scripted results per label (`ScanResult`, `UNCERTAIN`, `STOP` or a callable); honours `receipt`
    (no upload), calls `on_receipt` after an accepted upload, records every call. Default: complete, no findings."""

    def __init__(self, script: dict[str, list[Any]] | None = None):
        self.script = {k: list(v) for k, v in (script or {}).items()}
        self.calls: list[ScanCall] = []
        self.uploads = 0

    def __call__(self, client: Any, zip_path: Path, zip_sha256: str, label: str, project_name: str, *,
                 receipt: UploadReceipt | None = None, on_receipt: Callable[[UploadReceipt], None] | None = None,
                 stop: Callable[[], bool] | None = None, journal: Any = None) -> ScanResult:
        items = self.script.get(label) or []
        item = items.pop(0) if items else scan_result()
        uploaded = receipt is None
        self.calls.append(ScanCall(label, zip_sha256, receipt, uploaded))
        if callable(item) and not isinstance(item, ScanResult):
            return item(zip_path=zip_path, zip_sha256=zip_sha256, label=label, receipt=receipt, on_receipt=on_receipt)
        actual = file_sha(zip_path)
        if uploaded:
            self.uploads += 1
            if item == UNCERTAIN:
                raise IvasUploadUncertain("connection reset after the upload was sent")
            receipt = UploadReceipt(project_id=f"prj-{label}", analysis_id=f"anl-{label}-{self.uploads}",
                                    zip_sha256=actual, project_name=project_name)
            if on_receipt is not None:
                on_receipt(receipt)
        if item == STOP:
            return ScanResult(label=label, zip_sha256=actual, complete=False, receipt=receipt,
                              incomplete_reasons=["stop_requested"])
        res = ScanResult.from_dict(item.to_dict())
        res.label = label
        res.zip_sha256 = res.zip_sha256 or actual
        res.receipt = receipt
        return res


def fake_scan(script: dict[str, list[Any]] | None = None) -> FakeScanner:
    """Factory for a `FakeScanner`."""
    return FakeScanner(script)


# ---------------------------------------------------------------------------- Maven
class FakeMaven:
    """A `MavenGateway`: readiness list, scripted `package` outcomes per call (default ok), scripted `resolve` models.
    `package` writes `target/built.txt` inside the project dir (to prove accepted/ stays clean)."""

    def __init__(self, readiness: list[str] | None = None, outcomes: list[bool | BuildOutcome] | None = None,
                 models: list[ResolvedModel] | None = None, packaged: list[PackagedLib] | None = None,
                 on_package: Callable[[Path], None] | None = None):
        self._readiness = list(readiness or [])
        self.outcomes = list(outcomes or [])
        self.models = list(models or [])
        self._packaged = list(packaged or [])
        self.on_package = on_package
        self.package_calls: list[Path] = []
        self.resolve_calls: list[Path] = []
        self.targets_seen: list[tuple[str, list[str] | None]] = []     # (call, targets kw) — None = not passed

    def readiness(self) -> list[str]:
        return list(self._readiness)

    def allowed_hosts(self) -> list[str]:
        return ["nexus.invalid"]

    def package(self, project: Path, log_path: Path, stop: Callable[[], bool] | None = None,
                targets: list[str] | None = None) -> BuildOutcome:
        self.package_calls.append(project)
        self.targets_seen.append(("package", targets))
        if self.on_package is not None:
            self.on_package(project)
        (project / "target").mkdir(parents=True, exist_ok=True)
        (project / "target" / "built.txt").write_text("built", encoding="utf-8")
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("[INFO] BUILD\n", encoding="utf-8")
        item = self.outcomes.pop(0) if self.outcomes else True
        if isinstance(item, BuildOutcome):
            return item
        return BuildOutcome(ok=bool(item), exit_code=0 if item else 1, log=log_path.name,
                            summary="" if item else "[ERROR] COMPILATION ERROR")

    def resolve(self, project: Path, work: Path, targets: list[str] | None = None) -> ResolvedModel:
        self.resolve_calls.append(project)
        self.targets_seen.append(("resolve", targets))
        return self.models.pop(0) if self.models else ResolvedModel()

    def packaged(self, project: Path, targets: list[str] | None = None) -> list[PackagedLib]:
        self.targets_seen.append(("packaged", targets))
        return list(self._packaged)


class FakeVersions:
    """A `VersionSource` with a fixed version list."""

    def __init__(self, versions: dict[str, list[str]] | None = None):
        self._v = dict(versions or {})

    def versions(self, group_id: str, artifact_id: str) -> list[str]:
        return list(self._v.get(f"{group_id}:{artifact_id}", []))

    def evidence(self, group_id: str, artifact_id: str, version: str) -> str:
        return "repository:nexus.invalid" if version in self.versions(group_id, artifact_id) else ""


# ---------------------------------------------------------------------------- inventory / plan / apply
class ScriptedInventory:
    """An `InventoryFn`: scripted `Inventory` per call, default one direct item per finding."""

    def __init__(self, results: list[Inventory] | None = None):
        self.results = list(results or [])
        self.calls: list[list[Finding]] = []
        self.inhouse: list[dict[str, str]] = []          # the in-house table the graph passed per call

    def __call__(self, project: Path, findings: list[Finding], resolved: ResolvedModel, *,
                 inhouse: Any = None) -> Inventory:
        self.calls.append(list(findings))
        self.inhouse.append(dict(inhouse or {}))
        if self.results:
            return self.results.pop(0)
        return Inventory(items=[InventoryItem(ga=f.identifier, version=f.version, origin="direct_literal",
                                              finding_ids=[f.id]) for f in findings])


def proposal(n: int = 1, *, round: int = 1, ga: str = "example:lib", old: str = "1.0", new: str = "1.1",
             line: int = 0, decision: str = D_AUTO, pom: str = "pom.xml", risks: list[str] | None = None) -> Proposal:
    """A proposal editing `<version>old</version>` in `pom` (line found by `ScriptedPlanner` when 0)."""
    p = Proposal(id=f"p-{round}-{n}", round=round, ga=ga, from_version=old, to_version=new, kind="direct_version",
                 risks=list(risks or []), requires_approval=decision != D_AUTO, decision=decision,
                 evidence="repository:nexus.invalid", finding_ids=["CVE-2026-0001"],
                 edits=[TextEdit(pom=pom, line=line, old=old, new=new)])
    return p


class ScriptedPlanner:
    """A `PlanFn`: scripted `RoundPlan`s (or callables) per call; stamps round / hashes, finds edit lines, diffs."""

    def __init__(self, plans: list[RoundPlan | Callable[..., RoundPlan]] | None = None, policy_hash: str = POLICY_HASH):
        self.plans = list(plans or [])
        self.policy_hash = policy_hash
        self.calls: list[dict[str, Any]] = []

    def __call__(self, project: Path, inventory: Inventory, cfg: Any, source: Any, *, round: int,
                 source_tree_hash: str, rejected: set[tuple[str, str]] | None = None) -> RoundPlan:
        self.calls.append({"round": round, "source_tree_hash": source_tree_hash, "rejected": set(rejected or set())})
        item = self.plans.pop(0) if self.plans else RoundPlan(round=round, source_tree_hash="", policy_hash="")
        plan = item(project=project, round=round) if callable(item) else RoundPlan.from_dict(item.to_dict())
        plan.round, plan.source_tree_hash, plan.policy_hash = round, source_tree_hash, self.policy_hash
        for p in plan.proposals:
            p.round, p.source_tree_hash, p.policy_hash = round, source_tree_hash, self.policy_hash
            for e in p.edits:
                if not e.line:
                    lines = (project / e.pom).read_text(encoding="utf-8").splitlines()
                    e.line = next((i + 1 for i, ln in enumerate(lines) if f">{e.old}<" in ln), 1)
            p.proposal_hash = p.compute_hash()
        return plan


def _apply_edit(text: str, e: TextEdit) -> str | None:
    lines = text.splitlines(keepends=True)
    if not (1 <= e.line <= len(lines)) or lines[e.line - 1].count(e.old) != 1:
        return None
    lines[e.line - 1] = lines[e.line - 1].replace(e.old, e.new)
    return "".join(lines)


class FakeApply:
    """An `ApplyFns`: real line edits + difflib patch; scripted verification lines."""

    def __init__(self, text_violations: list[str] | None = None, resolution_lines: list[str] | None = None,
                 skip: dict[str, str] | None = None):
        self.text_violations = list(text_violations or [])
        self.resolution_lines = list(resolution_lines or [])
        self.skip = dict(skip or {})
        self.applied_calls: list[list[str]] = []

    def apply_proposals(self, project: Path, proposals: list[Proposal]) -> ApplyResult:
        self.applied_calls.append([p.id for p in proposals])
        res = ApplyResult()
        patches: list[str] = []
        for p in proposals:
            if p.id in self.skip:
                res.skipped[p.id] = self.skip[p.id]
                continue
            ok = True
            for e in p.edits:
                f = project / e.pom
                old = f.read_text(encoding="utf-8")
                new = _apply_edit(old, e)
                if new is None:
                    ok = False
                    break
                f.write_text(new, encoding="utf-8")
                patches.extend(difflib.unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True),
                                                    f"a/{e.pom}", f"b/{e.pom}"))
                if e.pom not in res.changed_files:
                    res.changed_files.append(e.pom)
            if ok:
                res.applied.append(p.id)
            else:
                res.skipped[p.id] = "edit line did not match"
        res.patch = "".join(patches)
        return res

    def verify_text_only(self, original: Path, candidate: Path, proposals: list[Proposal], paths: list[str]) -> list[str]:
        return list(self.text_violations)

    def verify_resolution(self, proposals: list[Proposal], before: ResolvedModel, after: ResolvedModel,
                          packaged: list[PackagedLib]) -> list[str]:
        return list(self.resolution_lines)


def make_components(**overrides: Any) -> Components:
    """A `Components` bundle of fakes; any field can be overridden."""
    base: dict[str, Any] = {"ivas": FakeIvas(), "scan": FakeScanner(), "maven": FakeMaven(),
                            "versions": FakeVersions(), "intake": FakeIntakeFns(), "inventory": ScriptedInventory(),
                            "plan": ScriptedPlanner(), "apply": FakeApply()}
    base.update(overrides)
    return Components(**base)


# ---------------------------------------------------------------------------- run directory
POM = """<project>
  <modelVersion>4.0.0</modelVersion>
  <groupId>example</groupId>
  <artifactId>app</artifactId>
  <version>0.1</version>
  <dependencies>
    <dependency>
      <groupId>example</groupId>
      <artifactId>lib</artifactId>
      <version>1.0</version>
    </dependency>
  </dependencies>
</project>
"""
DEFAULT_FILES = {"pom.xml": POM, "src/main/java/App.java": "class App {}\n"}


def make_run(tmp_path: Path, files: dict[str, str] | None = None, *, cfg: Any = None,
             policy_hash: str = POLICY_HASH) -> tuple[RemediationState, RunPaths]:
    """Build original/source.zip + manifest + accepted/ by hand and return a fresh QUEUED state + its paths."""
    from sbmig.remediations.graph import new_state

    files = dict(DEFAULT_FILES if files is None else files)
    paths = RunPaths(tmp_path / "run")
    raw = {k: v.encode("utf-8") for k, v in files.items()}
    paths.original_zip.parent.mkdir(parents=True, exist_ok=True)
    paths.original_zip.write_bytes(deterministic_zip(raw))
    entries = [{"path": k, "size": len(v), "sha256": hashlib.sha256(v).hexdigest()} for k, v in sorted(raw.items())]
    paths.manifest.write_text(json.dumps({"schema": 1, "entries": entries}), encoding="utf-8")
    for k, v in raw.items():
        f = paths.accepted / k
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(v)
    intake = FakeIntakeFns()
    intake_result = IntakeResult(original_zip="original/source.zip", zip_sha256=file_sha(paths.original_zip),
                                 manifest="original/manifest.json", accepted_dir="accepted", project_root=".",
                                 tree_hash=intake.tree_hash(paths.accepted, sorted(raw)), files=len(raw),
                                 unpacked_bytes=sum(len(v) for v in raw.values()))
    state = new_state("rem-1", "app.zip", cfg or make_cfg(), intake_result, ["default.yml"], policy_hash=policy_hash)
    return state, paths


@dataclass
class Recorder:
    """Collects `save` snapshots and journal lines; a switchable stop flag."""
    saves: list[dict[str, Any]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    stop_flag: bool = False
    stop_after_saves: int | None = None

    def save(self, state: RemediationState) -> None:
        self.saves.append(json.loads(json.dumps(state.to_dict())))

    def journal(self, event: dict[str, Any]) -> None:
        self.events.append(dict(event))

    def stop(self) -> bool:
        if self.stop_after_saves is not None and len(self.saves) >= self.stop_after_saves:
            return True
        return self.stop_flag
