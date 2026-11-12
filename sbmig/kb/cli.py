"""``sbmig kb …`` 명령(v2.1) — 사람이 KB 를 보고 고치는 창구.
오류 목록(``errors list | show``), 케이스와 수정 스크립트(``cases list | show | retire | activate | pin |
unpin``), KB 전체의 stats / validate / export / import / reindex. ``sbmig/cli.py`` 가 ``kb_cli`` 를 ``sbmig
kb`` 로 붙인다. Typer 는 함수 docstring 을 ``--help`` 로 쓰므로 각 명령 docstring 의 첫 줄(한국어)이 명령
목록의 요약이 된다 — 그래서 첫 줄은 한 줄로 짧게 둔다. 데이터는 전부 ``FileKnowledgeBase`` → ``CaseStore`` 를
거친다(쓰기는 KB 잠금 안에서). 고치는 법: 나쁜 스크립트는 ``cases retire``, 사람 몫 케이스는 ``cases pin <id>
block:<이유>``, 기록을 다시 셀 때는 ``reindex``.

`sbmig kb …` (v2.1, docs/v2/PLAN-v2.1.md §3.4): the error list of every run (`errors list | show`), the cases and
their fix scripts (`cases list | show | retire | activate | pin | unpin`), plus stats / validate / export / import /
reindex of the whole KB."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import typer
from pydantic import ValidationError

from sbmig.core.config import Config, load_config
from sbmig.errors.cases import CaseStore, entry_lines, run_line, scripts_text, valid_case_id
from sbmig.errors.entries import fmt_entry, tries_text

from .store import FileKnowledgeBase

kb_cli = typer.Typer(
    help="File knowledge base (v2.1): every run's error list (`errors`), cases with their fix scripts (`cases`); "
         "stats, validate, export / import, reindex.",
    no_args_is_help=True)
errors_cli = typer.Typer(help="Every run's error entries (<kb>/errors.jsonl).", no_args_is_help=True)
cases_cli = typer.Typer(help="Cases (one per L2 signature) and their fix scripts (<kb>/cases/<case_id>/).",
                        no_args_is_help=True)
kb_cli.add_typer(errors_cli, name="errors")
kb_cli.add_typer(cases_cli, name="cases")


def _load_cfg(configs: list[Path]) -> Config:
    """KB 명령은 회사 artifacts 설정이 필요 없으므로, 설정에 없으면 자리표시자로 채워 검증 오류를 피한다.

    KB commands do not need the company artifacts section; use a placeholder when no config provides it."""
    try:
        return load_config(*configs)
    except ValidationError as e:
        if not any(err.get("loc") == ("artifacts",) for err in e.errors()):
            raise
        return load_config(*configs, overrides={"artifacts": {"parent": {"group_id": "unset", "artifact_id": "unset"}}})


def _kb(ctx: typer.Context) -> FileKnowledgeBase:
    """명령이 쓸 FileKnowledgeBase: 상위 CLI 가 ctx.obj 에 넣어
    둔 cfg / kb 를 쓰고, 없으면 --config / --kb-dir 로 만든다.

    The parent CLI may pre-populate ctx.obj["cfg"] (a loaded Config) or ctx.obj["kb"]."""
    obj = ctx.ensure_object(dict)
    if "kb" not in obj:
        cfg: Config = obj.get("cfg") or _load_cfg(obj.get("configs") or [])
        kb_dir = obj.get("kb_dir")
        obj["kb"] = FileKnowledgeBase(cfg, kb_dir=Path(kb_dir) if kb_dir else None)
    return obj["kb"]


def _fail(msg: str) -> None:
    typer.echo(msg, err=True)
    raise typer.Exit(1)


@kb_cli.callback()
def _main(
    ctx: typer.Context,
    config: list[Path] = typer.Option([], "--config", "-c", help="Extra config yml (repeatable)."),
    kb_dir: Optional[Path] = typer.Option(None, "--kb-dir", help="Override kb.dir from the config."),
) -> None:
    obj = ctx.ensure_object(dict)
    if config:
        obj["configs"] = list(config)
    if kb_dir:
        obj["kb_dir"] = kb_dir


# --------------------------------------------------------------------------- whole KB
RESIGN_LINES = 50                 # moved cases listed by `kb reindex` before "… N more"


@kb_cli.command("reindex")
def reindex_cmd(ctx: typer.Context,
                dry_run: bool = typer.Option(False, "--dry-run",
                                             help="Only print what the re-sign would move; write nothing.")) -> None:
    """재색인: 옛 서명 규칙의 기록을 지금 케이스 ID 로 옮기고(재서명) 케이스 수치·실행 기록을 다시 만든다.

    Re-sign the records signed by older signature rules (moved to the case ids the current rules give), then
    recount every case (seen / resolved / projects / last solution) from errors.jsonl and rewrite cases.md; rebuild
    every stored script's run history (why it failed) and give old entries their as-is / to-be code. ``--dry-run``
    prints what would move and writes nothing (no recount either)."""
    kb = _kb(ctx)
    if dry_run:
        rep = kb.resign(dry_run=True)
        for ln in rep.lines(RESIGN_LINES):
            typer.echo(ln)
        if rep.error:
            raise typer.Exit(1)
        return
    n = kb.reindex()
    rep = kb.last_resign
    if rep is not None:
        for ln in rep.lines(RESIGN_LINES):
            typer.echo(ln)
    st = kb.stats()
    done = getattr(kb.cases, "last_reindex", None) or {}
    # 변경 조각 수와 해결한 시도 수는 따로 센 숫자(A15) — INFO 줄과 같은 말로 둘 다 보인다
    typer.echo(f"{n} case(s), {st['script_versions']} script version(s); {done.get('runs', 0)} script run "
               f"record(s) rebuilt, {done.get('hunks', 0)} entr(ies) given as-is/to-be hunks, "
               f"{done.get('resolved_by', 0)} build-resolved entr(ies) given a probable fixer — {kb.cases.md_path}")


@kb_cli.command("stats")
def stats_cmd(ctx: typer.Context, as_json: bool = typer.Option(False, "--json")) -> None:
    """KB 숫자: 오류 항목, 케이스, 상태별 스크립트 버전, 고정된 케이스.

    Counts: error entries, cases, script versions by status, pinned cases."""
    st = _kb(ctx).stats()
    if as_json:
        typer.echo(json.dumps(st, indent=2, ensure_ascii=False))
        return
    for k, v in st.items():
        typer.echo(f"{k:<20} {v}")


@kb_cli.command("validate")
def validate_cmd(ctx: typer.Context) -> None:
    """KB 파일 점검(errors.jsonl 줄, case.json, 스크립트 파일, pin). 문제가 있으면 exit 1.

    Check the KB files (errors.jsonl lines, case.json files, script files, pins). Exit 1 on a problem."""
    kb = _kb(ctx)
    problems = kb.validate()
    for p in problems:
        typer.echo(f"PROBLEM: {p}")
    if problems:
        typer.echo(f"{len(problems)} problem(s) in {kb.root}")
        raise typer.Exit(1)
    st = kb.stats()
    typer.echo(f"OK: {st['errors']} error entr(ies), {st['cases']} case(s), {st['script_versions']} script "
               f"version(s) in {kb.root}")


@kb_cli.command("export")
def export_cmd(ctx: typer.Context, out: Path = typer.Argument(..., metavar="OUT.zip")) -> None:
    """KB 를 zip 하나로 내보낸다(다른 PC 로 옮길 때).

    Export the KB as a zip (cases/, errors.jsonl, cases.md) to move it between machines."""
    typer.echo(f"exported {_kb(ctx).export_zip(out)}")


@kb_cli.command("import")
def import_cmd(ctx: typer.Context, src: Path = typer.Argument(..., metavar="IN.zip", exists=True),
               replace: bool = typer.Option(False, "--replace", help="Replace the local KB (kept as .bak-*).")) -> None:
    """내보낸 KB 를 가져온다(기본은 합치기, --replace 는 통째 교체하고 예전 KB 는 .bak-* 로 남김).

    Import an exported KB: merged by default (new entries, unknown cases, scripts by their code)."""
    counters = _kb(ctx).import_zip(src, merge=not replace)
    typer.echo(", ".join(f"{k} {v}" for k, v in counters.items()))


# --------------------------------------------------------------------------- errors
@errors_cli.command("list")
def errors_list_cmd(
    ctx: typer.Context,
    run: str = typer.Option("", "--run", help="Only this run id."),
    case: str = typer.Option("", "--case", help="Only this case (id or prefix)."),
    status: str = typer.Option("", "--status", help="open | pom-verify | resolved | blocked | unresolved | note"),
    project: str = typer.Option("", "--project"),
    limit: int = typer.Option(50, "--limit", help="Newest N (0 = all)."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """오류 항목을 한 줄씩: ID, 실행, 상태, 케이스, 파일:줄 메시지, 시도, 해결(기본 최근 50개).

    One line per entry: id, run, status, case, file:line message, tries, resolved."""
    rows = list(_kb(ctx).cases.iter_errors(run=run, case=case, status=status, project=project))
    total = len(rows)
    if limit > 0:
        rows = rows[-limit:]
    if as_json:
        typer.echo(json.dumps([json.loads(e.model_dump_json()) for e in rows], indent=2, ensure_ascii=False))
        return
    for e in rows:
        typer.echo(f"{e.id}  {e.run_id or '-'}  {e.status:<10} {e.case_id or '-':<10}  {fmt_entry(e)[:140]}"
                   + (f"  [{tries_text(e)}]" if e.tries else "") + (f"  → {e.resolved}" if e.resolved else ""))
    typer.echo(f"{len(rows)} of {total} entr(ies)")


@errors_cli.command("show")
def errors_show_cmd(ctx: typer.Context, entry: str = typer.Argument(..., metavar="ID", help="Entry id or prefix."),
                    diff: bool = typer.Option(True, "--diff/--no-diff")) -> None:
    """항목 하나와 모든 시도: 원인 / 해결 / 수정(수정 전·후 코드) / 바뀐 파일 / diff.

    The entry with every try: 원인 / 해결 / applied files / diff."""
    hits = list(_kb(ctx).cases.iter_errors(entry=entry))
    if not hits:
        _fail(f"no entry {entry}")
    ids = list(dict.fromkeys(e.id for e in hits))
    if len(ids) > 1:
        _fail(f"{entry} matches {len(ids)} entries: " + ", ".join(ids[:10]))
    for ln in entry_lines(hits[-1], diff=diff):
        typer.echo(ln)


# --------------------------------------------------------------------------- cases
def _case_id(store: CaseStore, key: str, *, versioned: bool = False) -> str:
    """케이스 ID, 또는 하나로 정해지는 4자 이상의 앞부분 → 케이스 ID(없거나 여럿이면 exit 1). 폴더가 없는 ID 가
    어떤 케이스의 ``aliases``(재서명으로 옮겨 온 옛 ID)에 있으면 그 케이스(stderr 에 ``case <old> is now <new>``).

    A case id or a unique prefix of 4+ characters; an id (or prefix) that names no folder but is in a case's
    ``aliases`` (moved there by a re-sign) resolves to that case (``case <old> is now <new>`` on stderr). A real
    folder always wins over an alias. ``versioned`` (retire / activate take a version number): an alias is refused —
    a merge may have renumbered the moved scripts."""
    ids = store.case_ids()
    if key in ids:
        return key
    hits = [c for c in ids if c.startswith(key)] if len(key) >= 4 else []
    if len(hits) == 1:
        return hits[0]
    if not hits:
        found = _alias_target(store, key, quiet=versioned)
        if found and versioned:
            # 합치기에서 번호가 바뀌었을 수 있다 — 옛 번호로 다른 스크립트를 끄지 않게 거부한다
            _fail(f"case {key} is now {found}: version numbers may have changed — look at 'sbmig kb cases show "
                  f"{found}' and use that id")
        if found:
            return found
    _fail(f"no case {key}" if not hits else f"{key} matches {len(hits)} cases: " + ", ".join(hits[:10]))
    return ""


def _alias_target(store: CaseStore, key: str, quiet: bool = False) -> str:
    """``key``(ID 또는 4자 이상 앞부분)를 aliases 에 가진 케이스 하나 — 없거나 여럿이면 "".

    The one case whose ``aliases`` hold ``key`` (an id or a 4+ character prefix); "" when none or several."""
    found: dict[str, str] = {}
    for c, _ in store.cases():
        for a in c.aliases:
            if a == key or (len(key) >= 4 and a.startswith(key)):
                found.setdefault(c.case_id, a)
    if len(found) != 1:
        return ""
    (new, old), = found.items()
    if not quiet:
        typer.echo(f"case {old} is now {new}", err=True)
    return new


@cases_cli.command("list")
def cases_list_cmd(ctx: typer.Context, as_json: bool = typer.Option(False, "--json")) -> None:
    """케이스를 한 줄씩(널리 퍼진 것 먼저): ID, 프로젝트 수, seen / resolved, 스크립트, pin, 이름.

    One line per case (most widespread first): id, projects, seen / resolved, scripts, pin, name."""
    cases = _kb(ctx).cases.cases()
    if as_json:
        typer.echo(json.dumps([{**c.model_dump(mode="json"), **x} for c, x in cases], indent=2, ensure_ascii=False))
        return
    for c, x in cases:
        typer.echo(f"{c.case_id}  projects {len(c.projects):<3} seen {c.seen:<4} resolved {c.resolved:<4} "
                   f"scripts [{scripts_text(c)}]" + (f"  pin {x['pin']}" if x.get("pin") else "")
                   + f"  {c.name or c.l2}")
    typer.echo(f"{len(cases)} case(s)")


@cases_cli.command("show")
def cases_show_cmd(ctx: typer.Context, key: str = typer.Argument(..., metavar="CASE_ID"),
                   code: bool = typer.Option(True, "--code/--no-code", help="Print the scripts."),
                   last: int = typer.Option(5, "--last", help="The last N entries of the case with their tries."),
                   diff: bool = typer.Option(False, "--diff/--no-diff")) -> None:
    """케이스 하나: case.json, 저장 스크립트(코드·계보·실행 기록), errors.jsonl 의 최근 항목.

    case.json, its scripts (code) and the last entries of the case from errors.jsonl."""
    store = _kb(ctx).cases
    cid = _case_id(store, key)
    case = store.find(cid)
    if case is None:
        _fail(f"case {cid}: case.json does not read (`sbmig kb validate`)")
    extra = store.extra(cid)
    typer.echo(f"case {case.case_id}  {case.name}")
    typer.echo(f"  l2: {case.l2 or '-'}  kind {case.kind.value}" + (f"  sig {case.sig}" if case.sig else ""))
    if case.aliases:
        typer.echo(f"  aliases: {', '.join(case.aliases)}")
    typer.echo(f"  seen {case.seen}, resolved {case.resolved}, projects {len(case.projects)}: "
               + (", ".join(case.projects) or "-"))
    for k, v in extra.items():
        typer.echo(f"  {k}: {v}")
    typer.echo(f"  KB 탐색: {store.describe(cid)}")
    if case.last_analysis:
        typer.echo(f"  last 원인: {case.last_analysis}")
    if case.last_solution:
        typer.echo(f"  last 해결: {case.last_solution}")
    for s in sorted(case.scripts, key=lambda s: s.version):
        typer.echo(f"  script v{s.version} ({s.file}) {s.status}, {s.source}, successes {s.successes} / failures "
                   f"{s.failures}, projects {', '.join(s.projects) or '-'}, made by {s.project or '-'} "
                   f"{s.created_by or ''}".rstrip() + (f", last {s.last_result}" if s.last_result else ""))
        # v2.1e: lineage, why it was made, why it failed, every run
        typer.echo(f"    parent: {f'v{s.parent}' if s.parent else '- (written new)'}")
        if s.reason:
            typer.echo(f"    reason: {s.reason}")
        if s.last_failure:
            typer.echo(f"    last failure: {s.last_failure}")
        if s.runs:
            typer.echo(f"    runs ({len(s.runs)}, newest last):")
            for r in s.runs:
                typer.echo(f"      {run_line(r)}")
        if code:
            body = store.code(cid, s.version)
            for ln in (body if body is not None else "(file missing)").splitlines():
                typer.echo(f"    | {ln}")
    entries = list(store.iter_errors(case=cid))
    # iter_errors 의 case 는 앞부분 일치라 정확히 같은 케이스만 남긴다
    entries = [e for e in entries if e.case_id == cid]
    if entries and last > 0:
        typer.echo(f"  last {min(last, len(entries))} of {len(entries)} entr(ies):")
        for e in entries[-last:]:
            for ln in entry_lines(e, diff=diff):
                typer.echo("  " + ln)


def _status(ctx: typer.Context, key: str, version: int, retire: bool) -> None:
    """retire / activate 명령의 공통 부분(없는 케이스·버전이면 exit 1)."""
    store = _kb(ctx).cases
    cid = _case_id(store, key, versioned=True)
    try:
        s = store.retire(cid, version) if retire else store.activate(cid, version)
    except KeyError as e:
        _fail(str(e).strip("'\""))
    typer.echo(f"case {cid} script v{s.version}: {s.status}")


@cases_cli.command("retire")
def cases_retire_cmd(ctx: typer.Context, key: str = typer.Argument(..., metavar="CASE_ID"),
                     version: int = typer.Argument(..., metavar="N")) -> None:
    """스크립트 버전 N 을 다시는 쓰지 않게 끈다(사람의 결정).

    Never run version N again (a person's decision)."""
    _status(ctx, key, version, True)


@cases_cli.command("activate")
def cases_activate_cmd(ctx: typer.Context, key: str = typer.Argument(..., metavar="CASE_ID"),
                       version: int = typer.Argument(..., metavar="N")) -> None:
    """retire 를 되돌린다(다른 프로젝트에서 해결한 적 있으면 proven, 아니면 unverified).

    Undo a retire (proven when it resolved the case in another project, else unverified)."""
    _status(ctx, key, version, False)


def _pin_target(store: CaseStore, key: str, create: bool) -> str:
    """``cases pin`` 이 고정할 케이스 ID: 아는 케이스(정확한 ID 또는 하나로 정해지는 앞부분), ``--create`` 면
    KB 가 아직 못 본 10자리 ID 도 그대로. 모르는 ID 는 exit 1 — 오타가 새 케이스 폴더를 만들지 않게(A19).

    The case ``cases pin`` pins: a known case (exact id or a unique prefix of 4+ characters); with ``create`` a
    10-character id the KB has not seen yet (set_pin makes its case.json). Anything else exits 1."""
    ids = store.case_ids()
    if key in ids:
        return key
    hits = [c for c in ids if c.startswith(key)] if len(key) >= 4 else []
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        _fail(f"{key} matches {len(hits)} cases: " + ", ".join(hits[:10]))
    found = _alias_target(store, key)                 # an old id a re-sign moved: the case it is now
    if found:
        return found
    if not create:
        _fail(f"unknown case {key}; use --create to pin a case the KB has not seen")
    # 미리 고정: 케이스 ID 는 L2 서명 sha1 의 앞 10자리 — 그 모양이 아니면 폴더를 만들지 않는다
    if len(key) != 10 or not valid_case_id(key):
        _fail(f"{key} is no case id (10 characters, as `sbmig kb cases list` shows them)")
    return key


@cases_cli.command("pin")
def cases_pin_cmd(ctx: typer.Context, key: str = typer.Argument(..., metavar="CASE_ID"),
                  spec: str = typer.Argument(..., metavar="block[:REASON]"),
                  create: bool = typer.Option(False, "--create",
                                              help="Pin a case id the KB has not seen yet (makes its case.json).")
                  ) -> None:
    """케이스를 `block` 으로 고정한다(스크립트·LLM 시도 없음, 인수인계 섹션이 이유를 보여 줌).

    Pin a case to `block` (never tried: no script, no LLM; Handover names the reason). An unknown id is refused
    unless --create is given."""
    store = _kb(ctx).cases
    # 모르는 ID 는 거부한다; 아직 폴더가 없는 케이스를 미리 고정할 때만 --create(set_pin 이 case.json 을 만든다)
    cid = _pin_target(store, key, create)
    try:
        store.set_pin(cid, spec)
    except ValueError as e:
        _fail(str(e))
    typer.echo(f"case {cid} pinned: {store.pin_of(cid)}")


@cases_cli.command("unpin")
def cases_unpin_cmd(ctx: typer.Context, key: str = typer.Argument(..., metavar="CASE_ID")) -> None:
    """케이스 고정을 푼다.

    Remove a case's pin."""
    store = _kb(ctx).cases
    cid = _case_id(store, key)
    store.set_pin(cid, "")
    typer.echo(f"case {cid} unpinned")
