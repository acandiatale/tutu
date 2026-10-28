"""``sbmig inventory`` 의 산출물 — inventory.md(``report.language`` 로 en | ko)와 inventory.json(언어 무관).
흐름상 위치: 마이그레이션 실행과 무관한 읽기 전용 명령이다. pom 이 없는 트리(Tomcat 에 풀린 JSP + Java)는
접수에서 거부되므로(회사 결정 2026-10-23), 담당팀에 보낼 거부 안내용으로 그 트리를 설명만 한다 — 아무것도
바꾸지 않는다. 핵심 진입점: ``render_inventory``(markdown 본문), ``write_inventory``(두 파일 쓰기). 문장은
모두 ``tr(lang, "…")`` 을 거친다(한국어 표는 i18n.py, 테스트가 누락을 잡는다).

`sbmig inventory` outputs: inventory.md (en | ko via `report.language`) + inventory.json (language independent).

Every text literal goes through `tr(lang, "…")` (Korean table in `i18n.py`, drift-tested); identifiers, paths,
jar names, coordinates and the analyzer's notes / warnings stay as they are.


"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from sbmig.analyze.webapp_inventory import jar_note
from sbmig.core.models import JarInfo, WebappInventory

from .i18n import normalize, tr


def _cell(s: object) -> str:
    # markdown 표 칸 안의 "|" 와 줄바꿈은 표를 깨뜨리므로 이스케이프 / 공백으로 바꾼다
    return str(s).replace("|", "\\|").replace("\n", " ")


def _code(s: Optional[str]) -> str:
    return f"`{s}`" if s else "`.`"


def _all_jars(inv: WebappInventory) -> list[JarInfo]:
    return [j for wr in inv.webapp_roots for j in wr.lib_jars]


def _java_levels(inv: WebappInventory) -> str:
    """WEB-INF/classes 의 class 파일에서 읽은 Java 수준들. "1.6" 과 "11" 이 섞여도 숫자 순으로 정렬한다."""
    levels = sorted({wr.java_version_from_classes for wr in inv.webapp_roots if wr.java_version_from_classes},
                    key=lambda v: int(v[2:]) if v.startswith("1.") else int(v))
    return ", ".join(levels) or "-"


def render_inventory(inv: WebappInventory, language: str = "en") -> str:
    """inventory.md 본문: 요약 표 → 경고 → 웹앱 루트별 표 → jar
    목록 → 소스 루트 → Maven 모듈 제안 → 다음 할 일."""
    lang = normalize(language)
    jars = _all_jars(inv)
    active = [j for j in jars if not j.ignored]
    known = sum(1 for j in active if j.artifact_guess)
    ignored_files = sum(len(wr.ignored_files) for wr in inv.webapp_roots)
    classes = sum(wr.classes_count for wr in inv.webapp_roots)
    L: list[str] = [tr(lang, "# Webapp inventory — {name}", name=Path(inv.root).name), ""]
    L.append(tr(lang, "Read-only scan of a directory without a pom (scanned {at}). Nothing was changed.",
                at=inv.scanned_at.strftime("%Y-%m-%d %H:%M UTC")))
    L.append("")
    L += [tr(lang, "| Item | Value |"), "|---|---|",
          tr(lang, "| Root | `{root}` |", root=inv.root),
          tr(lang, "| Webapp roots | {n} |", n=len(inv.webapp_roots)),
          tr(lang, "| JSP files | {n} |", n=sum(wr.jsp_count for wr in inv.webapp_roots)),
          tr(lang, "| Source roots / .java files | {roots} / {files} |", roots=len(inv.source_roots),
             files=sum(s.java_files for s in inv.source_roots)),
          tr(lang, "| Jars known / unknown / ignored | {known} / {unknown} / {ignored} |", known=known,
             unknown=len(active) - known, ignored=ignored_files),
          tr(lang, "| Class files (Java level) | {n} ({java}) |", n=classes, java=_java_levels(inv)),
          tr(lang, "| Duplicate classes | {n} |", n=inv.duplicate_classes),
          tr(lang, "| Build files found | {files} |",
             files=", ".join(f"`{f}`" for f in inv.build_files) or "-"),
          tr(lang, "| Warnings | {n} |", n=len(inv.warnings)), ""]

    if inv.warnings:
        L += [tr(lang, "## Warnings"), ""]
        L += [f"- {_cell(w)}" for w in inv.warnings]
        L.append("")

    L += [tr(lang, "## Webapp roots"), ""]
    if not inv.webapp_roots:
        L += [tr(lang, "No directory with a `WEB-INF` was found."), ""]
    for wr in inv.webapp_roots:
        L += [f"### {_code(wr.dir)}", "", tr(lang, "| Item | Value |"), "|---|---|",
              tr(lang, "| web.xml | {path} (Servlet {spec}) |", path=_code(wr.web_xml) if wr.web_xml else "-",
                 spec=wr.servlet_spec or "?"),
              tr(lang, "| Servlets / filters / listeners / taglibs | {s} / {f} / {l} / {t} |", s=wr.servlets,
                 f=wr.filters, l=wr.listeners, t=wr.taglibs),
              tr(lang, "| Frameworks | {fw} |", fw=", ".join(wr.frameworks) or "-"),
              tr(lang, "| JNDI resources | {names} |", names=", ".join(f"`{n}`" for n in wr.jndi_resources) or "-"),
              tr(lang, "| JSP files | {n} {sample} |", n=wr.jsp_count,
                 sample=("(" + ", ".join(f"`{s}`" for s in wr.jsp_sample) + ")") if wr.jsp_sample else ""),
              tr(lang, "| Static files | {n} |", n=wr.static_count),
              tr(lang, "| Spring XML | {files} |", files=", ".join(f"`{s}`" for s in wr.spring_xml) or "-"),
              tr(lang, "| WEB-INF/lib jars (ignored files) | {n} ({ignored}) |",
                 n=sum(1 for j in wr.lib_jars if not j.ignored), ignored=len(wr.ignored_files)),
              tr(lang, "| WEB-INF/classes | {n} class file(s), Java {java} |", n=wr.classes_count,
                 java=wr.java_version_from_classes or "-"),
              tr(lang, "| Classes without source | {n} {sample} |", n=wr.classes_without_source,
                 sample=("(" + ", ".join(f"`{s}`" for s in wr.classes_without_source_sample) + ")")
                 if wr.classes_without_source_sample else ""),
              ""]

    L += [tr(lang, "## Jars"), ""]
    if not jars:
        L += [tr(lang, "No jars under `WEB-INF/lib`."), ""]
    else:
        L += [tr(lang, "| Jar | Version | Coordinates guess | sha1 | Note |"), "|---|---|---|---|---|"]
        order = sorted(jars, key=lambda j: (j.ignored, j.artifact_guess is not None, j.path))
        for j in order:
            if j.ignored:
                note = tr(lang, "backup: Tomcat does not load it (left out of the pom)")
            elif not j.artifact_guess:
                note = tr(lang, "unknown coordinates")
            else:
                note = _cell(jar_note(j))
            coords = f"`{j.artifact_guess}`" if j.artifact_guess else "?"
            L.append(f"| `{j.path}` | {j.version_guess or '?'} | {coords} | `{j.sha1[:10]}` | {note} |")
        others = [f for wr in inv.webapp_roots for f in wr.ignored_files if f not in {j.path for j in jars}]
        if others:
            L += ["", tr(lang, "Other files under `WEB-INF/lib` (ignored): {files}",
                         files=", ".join(f"`{f}`" for f in others))]
        L.append("")

    L += [tr(lang, "## Source roots"), ""]
    if not inv.source_roots:
        L += [tr(lang, "No `.java` files were found."), ""]
    else:
        L += [tr(lang, "| Source root | Files | Packages | Encoding | javax imports | Frameworks | JDK internals | "
                       "Webapp |"), "|---|---|---|---|---|---|---|---|"]
        for s in inv.source_roots:
            javax = ", ".join(f"{k} {v}" for k, v in s.javax_imports.items()) or "-"
            fw = ", ".join(f"{k} {v}" for k, v in s.framework_imports.items()) or "-"
            L.append(f"| {_code(s.dir)} | {s.java_files} | {', '.join(s.packages) or '-'} | {s.encoding} | {javax} "
                     f"| {fw} | {s.jdk_internal_imports} | "
                     f"{_code(s.inside_webapp) if s.inside_webapp is not None else '-'} |")
        if inv.duplicate_classes:
            L += ["", tr(lang, "{n} class(es) exist in more than one source root: {sample}", n=inv.duplicate_classes,
                         sample=", ".join(f"`{c}`" for c in inv.duplicate_classes_sample))]
        L.append("")

    L += [tr(lang, "## Maven module proposal"), ""]
    if not inv.proposals:
        L += [tr(lang, "Nothing to propose (no webapp root and no sources)."), ""]
    else:
        L += [tr(lang, "A guess for the pom synthesis — change it if the split is wrong."), "",
              tr(lang, "| Module | Packaging | Webapp dir | Source dirs | Java | Jars known / unknown |"),
              "|---|---|---|---|---|---|"]
        for p in inv.proposals:
            L.append(f"| `{p.name}` | {p.packaging} | {_code(p.webapp_dir)} | "
                     f"{', '.join(_code(d) for d in p.source_dirs) or '-'} | {p.java_version} | "
                     f"{p.dependencies_known} / {p.dependencies_unknown} |")
        L.append("")
        for p in inv.proposals:
            if p.notes:
                L.append(f"**`{p.name}`**")
                L += [f"- {_cell(n)}" for n in p.notes]
                L.append("")

    L += [tr(lang, "## What to do next"), "",
          tr(lang, "1. Check the coordinates of the unknown jars and add the company ones to "
                   "`knowledge/libraries/jar_names.yml` (stem → groupId:artifactId), then re-run `sbmig inventory`."),
          tr(lang, "2. Confirm the module split above (one module per webapp root; shared sources need a common jar)."),
          tr(lang, "3. Round 2: `sbmig synthesize-pom` will write the poms from `inventory.json` (not available yet)."),
          ""]
    return "\n".join(L)


def write_inventory(inv: WebappInventory, out_dir: str | Path, cfg=None, *,
                    language: Optional[str] = None) -> dict[str, str]:
    """``out_dir`` 에 inventory.md 와 inventory.json 을 쓰고 두 경로를 돌려준다(``inv.outputs`` 에도 저장).

    Write inventory.md + inventory.json into `out_dir`; returns (and stores in `inv.outputs`) their paths."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    lang = normalize(language if language is not None else getattr(getattr(cfg, "report", None), "language", "en"))
    md, js = out / "inventory.md", out / "inventory.json"
    inv.outputs = {"inventory_md": str(md.resolve()), "inventory_json": str(js.resolve())}
    md.write_text(render_inventory(inv, lang), encoding="utf-8")
    js.write_text(inv.model_dump_json(indent=2), encoding="utf-8")
    return dict(inv.outputs)
