# Interfaces between workstreams

Source of truth: `sbmig/core/models.py`, `sbmig/core/interfaces.py`, `sbmig/core/config.py`, `sbmig/core/workspace.py`.
Do not edit these without a note here. If you need a new field, add it with a default so nobody breaks.

## Public entry points each WS must expose

```python
# A
from sbmig.analyze import analyze_project          # (ws: Workspace, cfg: Config) -> ProjectProfile
from sbmig.plan import build_plan                   # (profile, cfg, kb: KnowledgeBaseLike | None = None) -> MigrationPlan
from sbmig.plan import pre_handlers_for             # (profile, cfg) -> list[str]  handler ids to run pre-emptively

# B
from sbmig.restructure import PomEditor             # PomEditor(path: Path)  implements PomEditorLike
from sbmig.restructure import Restructurer          # Restructurer().apply(ws, profile, cfg, mode="layout"|"msa_pom") -> RestructureResult

# C
from sbmig.rewrite import OpenRewriteRunner         # OpenRewriteRunner(ws, cfg, kb=None) implements RewriteRunnerLike
from sbmig.build import MavenRunner                 # MavenRunner(ws, cfg) implements BuildRunnerLike
from sbmig.build import parse_maven_log             # (text: str, project_root: Path) -> list[BuildError]  (signatures filled)
from sbmig.build import signature_for               # (error: BuildError) -> tuple[str, str]  (L1, L2)

# D
from sbmig.fix import make_llm_client               # (cfg: Config) -> LLMClientLike   (provider switch, incl. FakeLLMClient)
from sbmig.fix import LLMFixer                      # LLMFixer(cfg, client, notes_dir=None) implements FixerLike
from sbmig.fix import PatchApplier                  # PatchApplier(ws).apply(changes) -> (applied, errors)
from sbmig.fix import parse_fix_response            # (text) -> ParsedResponse(changes, analysis, need_files, need_grep)

# E
from sbmig.triage import Triager                    # Triager(cfg) implements TriagerLike
from sbmig.triage import HandlerRegistry            # HandlerRegistry.default(cfg) -> registry; .get(id), .matching(cluster, profile, cfg)
from sbmig.loop import Orchestrator                 # Orchestrator(cfg, ws, ...).run() -> RunReport
from sbmig.report import write_report               # (report: RunReport, ws) -> dict[str, str] paths
# sbmig/cli.py                                       typer app: run | analyze | plan | build | rewrite | kb ...

# F
from sbmig.kb import FileKnowledgeBase              # FileKnowledgeBase(cfg) implements KnowledgeBaseLike
from sbmig.kb import kb_cli                         # typer sub-app mounted by sbmig/cli.py as `sbmig kb`
```

## Conventions
* All file paths inside models are **relative to `ws.root`** (posix separators). Logs/outputs are absolute.
* Never print; use `logging.getLogger("sbmig.<pkg>")`. The CLI configures rich logging.
* Subprocesses: always `cwd=ws.root` (or module dir), `capture_output`, timeout from config, write full log to
  `ws.new_log_path(label)`.
* Every mutation of the project goes through `ws.write/move/delete` or a `PomEditor` and is followed by
  `ws.commit(...)` **by the orchestrator** (components do not commit themselves, except Restructurer which may
  commit intermediate steps for clarity).
* Tests: `tests/<ws>/`, pure-python by default; mark `@pytest.mark.integration` for anything needing mvn/java/network.
* Python ≥ 3.10, type hints everywhere, pydantic v2.

## Change log
* 2026-10-18 PM: initial contracts.
* 2026-10-18 WS-F: `FixAttempt.cluster_errors: list[BuildError] = []` (default empty). The orchestrator should fill it
  with the cluster's errors when it creates an attempt; KB harvest needs them for L2 signatures and sanitized error
  copies. Without it harvest falls back to L1-only cases with errors synthesized from `cluster_signatures`.
* 2026-10-18 PM: `BuildConfig.external_package_prefixes` (default []) + `sbmig.build.register_external_prefixes` / `external_prefixes_from_config`; `MavenRunner.__init__` registers in-house group ids so they are not masked in signatures.
* 2026-10-18 PM: default `openrewrite.recipe_bom_version` pinned to 3.37.0 (3.38.0 references unpublished artifacts).
* 2026-10-18 PM: `@ServletComponentScan` FQCN verified as `org.springframework.boot.web.server.servlet.context.ServletComponentScan` (Boot 4.1.0).
* 2026-10-18 PM (integration): `BuildFixLoop` fills `FixAttempt.cluster_errors`; new handler `add-known-dependency` (`sbmig/triage/handlers/known_packages.yml`); project recipe `com.company.migrate.Boot4JacksonTwins` (Boot Jackson types → Jackson 3 twins) added to the `spring_boot_4` set; Restructurer copies as-is test/runtime-scoped deps into boot-jar when `@SpringBootTest` classes move and bumps optional artifacts through the conventional `<artifactId>.version` property when present.
* 2026-10-18 PM (integration 2): `RewriteResult.recipes_applied` / `forked`; `RewriteRunnerLike.run(..., fork=None)` (False → runNoFork); `KBConfig.try_candidates_in_plan`; planner appends candidate KB recipes to `spring.rewrite`; `Orchestrator._credit_plan_recipes` credits KB recipes by phase build outcome; fix loop runs reactive KB batches with `fork=False`.
