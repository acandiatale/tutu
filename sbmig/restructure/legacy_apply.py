"""재구성 단계에서 legacy Spring 설정(XML 설정, web.xml)을 Boot 앱에 맞게 고치는 모듈.
``Restructurer``가 이 믹스인을 상속한다.

A Spring Framework or servlet project turned into a Boot app can build and still fail at startup: bean
overriding is off, there is no JNDI, WEB-INF is not on the classpath, CommonsMultipartResolver is gone, the
default servlet and @EnableWebMvc behave differently. This mixin handles those places. Called from
``_generate_application``, ``_write_boot``, ``_web_xml_registrations`` and the end of ``apply``. Results go
to ``RestructureResult`` (``xml_imports``, ``conversion_facts`` and others), read by the fix loop and the
Handover. Switches are ``layout.legacy_context.*``; a switch that is off leaves only the Handover sentence.
As-is files are never edited; only the copies under ``legacy-config/`` are.

Contract: docs/INTERFACES.md "v2.1x". Pure text helpers live in `legacy_context`; the class / property tables
in `knowledge/config/legacy_context_rules.yml`.
"""
from __future__ import annotations

import logging
import posixpath
import re
from typing import TYPE_CHECKING, Any, Optional

from sbmig.core.models import CaseType

from . import legacy_context as lc
from .pom_editor import PomEditError, PomEditor

if TYPE_CHECKING:  # pragma: no cover
    from .restructurer import _Ctx

log = logging.getLogger("sbmig.restructure")

LEGACY_DIR = "legacy-config"
STASH = "asis-webinf"                       # restructurer.WEBINF_STASH와 같은 값이어야 한다
NON_LEGACY_CASES = (CaseType.SPRING_BOOT, CaseType.MSA)


class LegacyContextMixin:
    """legacy 컨텍스트 처리 메서드를 모은 믹스인 클래스.

    Relies on the Restructurer helpers `_append_props`, `_boot_resource_files`, `_jsp_web_path`,
    `_project_poms`, `_servlet_scan`."""

    # ------------------------------------------------------------------ 공통
    @staticmethod
    def _lc_switches(c: "_Ctx") -> Any:
        """``layout.legacy_context`` 설정을 반환한다. 없으면 기본값 객체를 반환한다."""
        from sbmig.core.config import LegacyContextConfig

        return getattr(c.cfg.layout, "legacy_context", None) or LegacyContextConfig()

    @staticmethod
    def _lc_rules(c: "_Ctx") -> lc.LegacyContextRules:
        """legacy 컨텍스트 규칙 표를 반환한다. 재구성 한 번에 한 번만 읽는다."""
        if c.legacy_rules is None:
            c.legacy_rules = lc.load_rules(c.cfg.resolve_path(lc.RULES_FILE))
        return c.legacy_rules

    @staticmethod
    def _lc_fact(c: "_Ctx", key: str, value: Any) -> None:
        """``conversion_facts``에 항목 하나를 넣는다. 값이 목록이면 중복 없이 이어 붙인다."""
        cf = c.result.conversion_facts
        if isinstance(value, list):
            cur = cf.setdefault(key, [])
            if isinstance(cur, list):
                cur.extend(v for v in value if v not in cur)
        else:
            cf[key] = value

    @staticmethod
    def _lc_legacy_case(c: "_Ctx") -> bool:
        """Boot 앱으로 바뀌는 legacy 프로젝트인지 반환한다.
        as-is가 Boot이거나 MSA면 False다. spring_legacy, servlet_tomcat, pure_java는 True다."""
        return c.profile.case_type not in NON_LEGACY_CASES

    def _lc_prop_configured(self, c: "_Ctx", boot: str, key: str) -> bool:
        """이 키가 이미 정해져 있으면 True를 반환한다.
        boot 모듈의 ``application*.properties``와 이번 실행에서 쓴 속성을 모두 본다."""
        if key in c.props_written:
            return True
        res = f"{boot}/src/main/resources"
        for f in self._boot_resource_files(c, res):                       # type: ignore[attr-defined]
            if not f.endswith(".properties"):
                continue
            if re.search(rf"(?m)^\s*{re.escape(key)}\s*[=:]", c.ws.read(f)):
                return True
        return False

    def _lc_write_props(self, c: "_Ctx", boot: str, header: str, rows: list[tuple[str, str, str]]) -> list[str]:
        """boot 모듈의 ``application.properties`` 끝에 ``# sbmig: <header>`` 블록을 덧붙인다.
        ``rows``는 (키, 값, 이유) 목록이다. 이미 정해진 키는 건너뛴다.
        덧붙인 ``키=값`` 목록을 반환한다. ``legacy_properties_written``에도 이유와 함께 남긴다."""
        new = [(k, v, why) for k, v, why in rows if not self._lc_prop_configured(c, boot, k)]
        seen: set[str] = set()
        new = [r for r in new if not (r[0] in seen or seen.add(r[0]))]
        if not new:
            return []
        lines = [f"# sbmig: {header}"]
        for k, v, why in new:
            if why:
                lines.append(f"# {why}")
            lines.append(f"{k}={v}")
            c.props_written[k] = v
        rel = f"{boot}/src/main/resources/application.properties"
        self._append_props(c, rel, lines)                                   # type: ignore[attr-defined]
        out = [f"{k}={v}" for k, v, _ in new]
        for (k, v, why) in new:
            entry = f"{k}={v}" + (f" ({why})" if why else "")
            if entry not in c.result.legacy_properties_written:
                c.result.legacy_properties_written.append(entry)
        return out

    def _lc_web_model(self, c: "_Ctx") -> Any:
        """첫 web.xml을 옮겨진 위치에서 읽어 그 모델을 반환한다. 한 번만 읽고, 없으면 None을 반환한다."""
        if c.web_model_loaded:
            return c.web_model
        c.web_model_loaded = True
        src = c.profile.source
        if not src.web_xml_paths:
            return None
        loc = c.new_location(src.web_xml_paths[0])
        if not c.ws.exists(loc):
            return None
        from sbmig.analyze.webxml import parse_web_xml

        c.web_xml_text = c.ws.read(loc)
        c.web_model = parse_web_xml(c.web_xml_text, src.web_xml_paths[0])
        return c.web_model

    # ------------------------------------------------------------------ 소스 모듈 검사
    def _lc_sources(self, c: "_Ctx", comp_dirs: list[str]) -> dict[str, list[str]]:
        """소스 모듈의 ``src/main/java``에서 Boot로 기동할 때 문제가 되는 코드를 찾는다.
        찾는 것은 @EnableWebMvc, WebApplicationInitializer, 기본 서블릿 처리, CommonsMultipartResolver 생성,
        ``/WEB-INF/``를 가리키는 ``@PropertySource``다. 한 번만 검사하고 결과를 재사용한다.

        Values are FQCNs, except `webinf_property_sources` (`file:line: value`)."""
        if c.lc_sources is not None:
            return c.lc_sources
        rules = self._lc_rules(c)
        out: dict[str, list[str]] = {"enable_web_mvc": [], "web_initializers": [], "default_servlet": [],
                                     "webinf_property_sources": [], "multipart_resolver": []}
        mp_simple = [x.rsplit(".", 1)[-1] for x in rules.multipart_classes]
        pats = {k: [re.compile(p) for p in ps] for k, ps in (("enable_web_mvc", rules.enable_web_mvc_java),
                                                             ("web_initializers", rules.web_initializer_java),
                                                             ("default_servlet", rules.default_servlet_java))}
        for d in comp_dirs:
            base = c.ws.abs(f"{d}/src/main/java")
            if not base.is_dir():
                continue
            for f in sorted(base.rglob("*.java")):
                try:
                    text = f.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                body = re.sub(r"//[^\n]*|/\*.*?\*/", "", text, flags=re.S)
                pm = re.search(r"^\s*package\s+([\w.]+)\s*;", body, re.M)
                fqcn = (pm.group(1) + "." if pm else "") + f.stem
                for key, rxs in pats.items():
                    if any(rx.search(body) for rx in rxs) and fqcn not in out[key]:
                        out[key].append(fqcn)
                # Java 코드의 CommonsMultipartResolver 생성은 고치지 않고 인수인계에만 남긴다. 수정 루프 노트가 다룬다
                if any(re.search(rf"\bnew\s+(?:[\w.]+\.)?{re.escape(n)}\s*\(", body) for n in mp_simple):
                    out["multipart_resolver"].append(fqcn)
                for m in re.finditer(r"@PropertySource\s*\(([^)]*)\)", body):
                    for v in re.findall(r'"(/?WEB-INF/[^"]+)"', m.group(1)):
                        line = body[:m.start()].count("\n") + 1
                        out["webinf_property_sources"].append(f"{c.ws.rel(f)}:{line}: {v}")
        c.lc_sources = out
        return out

    # ------------------------------------------------------------------ starter 선택
    def _lc_web_evidence(self, c: "_Ctx", asis_deps: Optional[list[Any]] = None) -> list[str]:
        """웹 앱이라는 근거 목록을 반환한다.
        web.xml, spring-webmvc, servlet-api, @Controller, JSP, 서블릿 클래스가 근거다.
        분석기가 찾은 라이브러리와 as-is 의존성을 모두 본다."""
        src = c.profile.source
        rules = self._lc_rules(c)
        ev: list[str] = []
        if src.web_xml_paths or src.has_web_xml:
            ev.append("web.xml")
        libs = {lib.name for lib in c.profile.libraries}
        for name, label in rules.web_libraries.items():
            if name in libs and label not in ev:
                ev.append(label)
        poms = [*c.profile.poms, *([c.profile.root_pom] if c.profile.root_pom else [])]
        keys = {d.key for d in (asis_deps or [])} | {d.key for p in poms for d in (p.dependencies or [])}
        for key, label in rules.web_artifacts.items():
            if key in keys and label not in ev:
                ev.append(label)
        if src.controllers:
            ev.append(f"@Controller ×{len(src.controllers)}")
        if src.jsp_count:
            ev.append(f"JSP ×{src.jsp_count}")
        if src.servlet_classes:
            ev.append(f"servlet class ×{len(src.servlet_classes)}")
        return ev

    def _lc_starter(self, c: "_Ctx", starter: Optional[str], asis_deps: Optional[list[Any]] = None) -> Optional[str]:
        """spring_legacy 프로젝트에 쓸 starter를 고른다. 웹 starter는 웹 앱 근거가 있을 때만 쓴다.
        근거가 없으면 ``starter_by_case['pure_java']``의 starter를 쓴다. 메모와 인수인계도 남긴다.
        고른 결과는 ``web_evidence``와 ``starter``에 기록한다.

        Switch: `layout.legacy_context.web_starter_by_evidence` (off = keep the web starter + a follow-up)."""
        L = c.cfg.layout
        ev = self._lc_web_evidence(c, asis_deps)
        c.result.web_evidence = ev
        web_starter = L.starter_by_case.get(CaseType.SPRING_LEGACY.value, "")
        plain = L.starter_by_case.get(CaseType.PURE_JAVA.value, "")
        if (c.profile.case_type == CaseType.SPRING_LEGACY and starter and starter == web_starter and not ev and plain
                and plain != starter and self._lc_switches(c).web_starter_by_evidence):
            c.note(f"starter {plain} instead of {starter} for {c.boot_rel}: no web evidence (no web.xml, spring-webmvc, "
                   "servlet-api, @Controller or JSP) — layout.legacy_context.web_starter_by_evidence")
            c.followup(f"no web evidence: {c.boot_rel} gets {plain}, not {starter} — the converted application starts no "
                       "embedded Tomcat (a CLI / batch Spring application); add the web starter only if it serves HTTP")
            starter = plain
        elif c.profile.case_type == CaseType.SPRING_LEGACY and starter and starter == web_starter and not ev and plain \
                and plain != starter:
            c.followup(f"no web evidence (no web.xml, spring-webmvc, servlet-api, @Controller or JSP), yet {c.boot_rel} "
                       f"gets {starter} (layout.legacy_context.web_starter_by_evidence: false) — the converted "
                       "application starts an embedded Tomcat")
        c.result.starter = starter
        return starter

    @staticmethod
    def _lc_asis_starters(c: "_Ctx") -> Optional[str]:
        """as-is pom들이 선언한 Boot starter를 한 줄 문자열로 반환한다. 없으면 None을 반환한다."""
        keys: list[str] = []
        poms = [*c.profile.poms, *([c.profile.root_pom] if c.profile.root_pom else [])]
        for p in poms:
            for d in p.dependencies or []:
                if d.group_id == "org.springframework.boot" and d.artifact_id.startswith("spring-boot-starter") \
                        and (d.scope or "") != "test" and d.artifact_id not in keys:
                    keys.append(d.artifact_id)
        return ("as-is: " + ", ".join(keys)) if keys else None

    # ------------------------------------------------------------------ XML 설정 복사와 import
    def _lc_xml_imports(self, c: "_Ctx", boot: str, comp_dirs: list[str]) -> list[str]:
        """web.xml ``contextConfigLocation``이 가리키는 진입 XML을 ``legacy-config/``에 복사한다.
        ``@ImportResource``에 넣을 목록을 반환한다.
        - ``<import>``로 연결된 파일은 상대 경로를 유지해 옆에 복사하고 목록에는 넣지 않는다
        - ``src/test``의 XML은 뺀다
        - web.xml이 없거나 진입 XML을 찾지 못하면 모든 XML을 import한다

        The copies are then edited (`_lc_edit_copies`) and the bean-override / circular-reference
        properties are written. Switches: `entry_xml_only`, `exclude_test_xml`."""
        LC = self._lc_switches(c)
        rules = self._lc_rules(c)
        src = c.profile.source
        res = c.result
        all_xml = list(src.xml_bean_configs)
        tests = [x for x in all_xml if lc.is_test_path(x)]
        pool = [x for x in all_xml if not (LC.exclude_test_xml and lc.is_test_path(x))]
        web_dirs = list(src.webapp_dirs or []) or ["src/main/webapp"]
        skipped: dict[str, str] = {t: "test" for t in tests} if LC.exclude_test_xml else {}
        entries: list[tuple[str, str]] = []
        unresolved: list[str] = []
        model = self._lc_web_model(c)
        if LC.entry_xml_only and model is not None:
            for role, locs in lc.entry_locations(model, rules):
                for loc in locs:
                    files, why = lc.resolve_location(loc, pool, web_dirs)
                    if not files:
                        unresolved.append(f"{loc} ({role}: {why})")
                    for f in files:
                        if f not in [e for e, _ in entries]:
                            entries.append((f, role))
                self._lc_fact(c, "contexts", [f"{role}: {', '.join(locs)}"])
        by_entry = bool(entries)
        if not entries:
            entries = [(x, "") for x in pool]
            if LC.entry_xml_only and model is not None:
                c.note("web.xml contextConfigLocation resolved to no XML bean config"
                       + (f" ({'; '.join(unresolved[:3])})" if unresolved else "")
                       + " — every XML bean config imported as before")
        elif unresolved:
            c.note("contextConfigLocation entries not resolved to a project file: " + "; ".join(unresolved[:4]))
        if not LC.exclude_test_xml and tests:
            c.followup(f"XML under src/test ({', '.join(tests[:3])}) is imported into the Boot context too "
                       "(layout.legacy_context.exclude_test_xml: false) — test-only beans now start with the application")
        if not LC.entry_xml_only and model is not None and pool:
            c.followup("every XML bean config is imported into the one Boot context (layout.legacy_context."
                       "entry_xml_only: false) — XML the as-is web.xml never loaded (other environments, tools, tests) "
                       "now creates beans too")
        res_dir = f"{boot}/src/main/resources"
        copies: dict[str, str] = {}           # as-is 경로: 복사본의 classpath 경로
        taken: dict[str, str] = {}            # 복사본의 classpath 경로: as-is 경로
        roles: dict[str, str] = {}
        listed: list[str] = []
        loaded: set[str] = set()
        reached_by: dict[str, str] = {}
        for asis, role in entries:
            if asis in loaded:                # 앞 진입 XML의 <import>로 이미 복사됐다
                skipped[asis] = f"reached by <import> from {posixpath.basename(reached_by.get(asis, '?'))}"
                continue
            cur = c.new_location(asis)
            if not c.ws.exists(cur):
                c.note(f"xml bean config {asis} not found at {cur}; not imported")
                continue
            if asis not in copies:
                name = posixpath.basename(cur)
                if f"{LEGACY_DIR}/{name}" in taken:
                    name = f"{posixpath.basename(posixpath.dirname(cur))}-{name}"
                cp = f"{LEGACY_DIR}/{name}"
                self._lc_copy(c, cur, f"{res_dir}/{cp}")
                copies[asis], taken[cp] = cp, asis
            roles[asis] = role
            listed.append(asis)
            loaded.add(asis)
            self._lc_follow_imports(c, asis, res_dir, all_xml if not LC.exclude_test_xml else pool, web_dirs,
                                    copies, taken, loaded, reached_by)
        for asis, frm in reached_by.items():
            if asis not in listed:
                skipped.setdefault(asis, f"reached by <import> from {posixpath.basename(frm)}")
        for x in pool:
            if x not in loaded and x not in skipped:
                skipped[x] = "not in contextConfigLocation" if by_entry else "not imported"
        imports = [f"classpath:{copies[a]}" for a in listed if a in copies]
        res.xml_imports = list(imports)
        res.xml_skipped = [f"{x} ({why})" for x, why in skipped.items()]
        self._lc_edit_copies(c, boot, res_dir, copies, roles, comp_dirs)
        if imports:
            reached = [a for a in copies if a not in listed]
            c.note(f"{len(imports)} XML bean config(s) copied to {c.boot_rel} {LEGACY_DIR}/ and imported"
                   + (f"; {len(reached)} reached by <import> copied next to them" if reached else "")
                   + (f"; not imported: {', '.join(res.xml_skipped[:4])}" if res.xml_skipped else ""))
            c.followup("XML bean configs imported via @ImportResource (" + ", ".join(imports) + ")"
                       + (" — the contexts web.xml declared" if by_entry else "")
                       + (f"; {len(reached)} file(s) they <import> were copied next to them" if reached else "")
                       + " — verify bean overlaps with Spring Boot auto-configuration")
            self._lc_legacy_properties(c, boot, copies)
            if by_entry and LC.handover_sentences and "root" in roles.values() and \
                    any(r.startswith("servlet") for r in roles.values()):
                c.followup("the root (ContextLoaderListener) and DispatcherServlet contexts are one Spring Boot context "
                           "now: aspects and <tx:annotation-driven/> of either XML apply to every bean, proxies are "
                           f"class-based, and @SpringBootApplication also scans all of {src.base_package or 'the base package'}"
                           " besides the XML component-scans")
        try:
            c.ws.journal({"event": "legacy_xml_imports", "imports": imports, "skipped": res.xml_skipped,
                          "by_entry": by_entry, "duplicate_bean_ids": res.duplicate_bean_ids})
        except (OSError, AttributeError) as e:  # pragma: no cover - journal unavailable
            log.debug("journal legacy_xml_imports skipped: %s", e)
        return imports

    def _lc_copy(self, c: "_Ctx", cur: str, dst: str) -> None:
        """파일을 바이트 그대로 복사한다. EUC-KR XML의 인코딩 선언이 계속 맞아야 하기 때문이다.
        같은 내용의 파일이 이미 있으면 그대로 둔다."""
        if c.ws.exists(dst):
            if c.ws.abs(dst).read_bytes() == c.ws.abs(cur).read_bytes():
                return
            c.ws.delete(dst)
        c.copy(cur, dst)
        c.legacy_copied_from.add(cur)
        if dst not in c.result.generated_files:
            c.result.generated_files.append(dst)

    def _lc_follow_imports(self, c: "_Ctx", asis: str, res_dir: str, cands: list[str], web_dirs: list[str],
                           copies: dict[str, str], taken: dict[str, str], loaded: set[str],
                           reached_by: dict[str, str]) -> None:
        """복사한 XML의 ``<import resource>``를 따라가며 연결된 파일도 그 옆에 복사한다.
        - 상대 경로는 복사한 위치 기준으로 같은 상대 경로에 둔다. 앞의 ``/``는 Spring이 무시한다
        - ``classpath:`` 파일 하나를 가리키거나 ``legacy-config/`` 밖을 가리키는 경로는 복사본을 가리키게 고친다
        - 여러 파일에 맞거나 ``classpath:``인 와일드카드 import는 그대로 두고 메모를 남긴다. Spring이 as-is 파일을 읽는다"""
        queue = [asis]
        while queue:
            imp = queue.pop(0)
            cp = copies.get(imp)
            if cp is None:
                continue
            path = f"{res_dir}/{cp}"
            text = c.ws.read(path)
            new_text = text
            for el in lc.elements(text, ("import",)):
                r = el.attrs.get("resource", "").strip()
                if not r or "${" in r:
                    continue
                low = r.lower()
                wildcard = "*" in r or "?" in r
                if low.startswith(("classpath:", "classpath*:")):
                    targets, _why = lc.resolve_location(r, cands, web_dirs)
                    absolute = True
                elif low.startswith(("file:", "http:", "https:")):
                    continue
                else:
                    want = posixpath.normpath(posixpath.join(posixpath.dirname(imp), r.lstrip("/")))
                    targets = [x for x in cands if lc.ant_match(want, x)] if wildcard else \
                        [x for x in cands if x == want]
                    # 분석기가 빈 설정으로 보지 못한 XML도 파일이 있으면 복사해 상대 import가 풀리게 한다
                    if not targets and not wildcard and c.ws.exists(c.new_location(want)):
                        targets = [want]
                    absolute = False
                if not targets:
                    continue
                if wildcard and (absolute or len(targets) > 1):
                    for t in targets:
                        reached_by.setdefault(t, imp)
                        loaded.add(t)
                    c.note(f"<import resource=\"{r}\"> in {cp} matches {len(targets)} file(s); left as it is (Spring "
                           "reads the as-is files from the classpath, the copies' edits do not apply to them)")
                    continue
                t = targets[0]
                reached_by.setdefault(t, imp)
                if t in copies:
                    tcp = copies[t]
                else:
                    tcp = posixpath.normpath(posixpath.join(posixpath.dirname(cp), r.lstrip("/"))) if not absolute \
                        else f"{LEGACY_DIR}/{lc.classpath_rel(t) or posixpath.basename(t)}"
                    if not tcp.startswith(LEGACY_DIR + "/") or (tcp in taken and taken[tcp] != t):
                        base = posixpath.basename(t)
                        tcp, i = f"{LEGACY_DIR}/{base}", 2
                        while tcp in taken and taken[tcp] != t:
                            tcp, i = f"{LEGACY_DIR}/{i}-{base}", i + 1
                    cur = c.new_location(t)
                    if not c.ws.exists(cur):
                        continue
                    self._lc_copy(c, cur, f"{res_dir}/{tcp}")
                    copies[t], taken[tcp] = tcp, t
                rel_ok = not absolute and posixpath.normpath(posixpath.join(posixpath.dirname(cp), r.lstrip("/"))) == tcp
                if not rel_ok:
                    new_r = posixpath.relpath(tcp, posixpath.dirname(cp))
                    tag = new_text[el.start:el.tag_end]
                    fixed = tag.replace(f'"{r}"', f'"{new_r}"').replace(f"'{r}'", f"'{new_r}'")
                    if fixed != tag:
                        new_text = new_text.replace(tag, fixed, 1)
                        c.note(f"{cp}: <import resource=\"{r}\"> → \"{new_r}\" (the copy of {t})")
                if t not in loaded:
                    loaded.add(t)
                    queue.append(t)
            if new_text != text:
                c.ws.write(path, new_text)
                c.changed.add(path)

    def _lc_edit_copies(self, c: "_Ctx", boot: str, res_dir: str, copies: dict[str, str], roles: dict[str, str],
                        comp_dirs: list[str]) -> None:
        """복사한 XML마다 Boot에 맞게 고치고, 찾은 내용을 모아 인수인계 문장을 남긴다.
        - 고치는 것: JNDI 빈 위 MIGRATION-TODO 주석, ``/WEB-INF`` 위치, ``${…}`` 위치,
          CommonsMultipartResolver 제거, JSP view prefix, ``<mvc:resources>`` 위치
        - 찾기만 하는 것: 기본 서블릿 설정, Security XML, 중복 빈 id"""
        LC = self._lc_switches(c)
        rules = self._lc_rules(c)
        res = c.result
        ids: dict[str, list[str]] = {}
        multipart_rows: list[tuple[str, str, str]] = []
        jndi: list[str] = []
        default_servlet: list[str] = []
        security: list[str] = []
        profile_lines: list[str] = []
        prefix_done: list[str] = []
        resources_lines: list[str] = []
        webinf_left: list[str] = []
        for cp in copies.values():
            path = f"{res_dir}/{cp}"
            try:
                text = c.ws.read(path)
            except OSError:
                continue
            orig = text
            for i in lc.bean_ids(text):
                ids.setdefault(i, [])
                if cp not in ids[i]:
                    ids[i].append(cp)
            # JNDI 빈은 담당 팀이 정하도록 그대로 두고 바로 위에 MIGRATION-TODO 주석만 단다
            for el in sorted(lc.elements(text, rules.jndi_elements) + lc.beans_of_class(text, rules.jndi_classes),
                             key=lambda e: e.start, reverse=True):
                bid = el.attrs.get("id") or el.attrs.get("name") or el.local
                jndi.append(f"{bid} ({cp})")
                if LC.handover_sentences:
                    text, _ = lc.insert_comment_above(text, el, rules.jndi_todo)
            # /WEB-INF는 Boot classpath에 없다. 위치를 classpath:asis-webinf/로 바꾸고, 파일이 없으면 복사해 둔다
            if LC.rewrite_webinf_locations:
                text, done = lc.rewrite_webinf_locations(text, rules, lambda sub: self._lc_stash(c, boot, sub))
                for old, new in done:
                    res.webinf_locations_rewritten.append(f"{cp}: {old} → {new}")
            else:
                webinf_left += [f"{cp}: {old}" for old, _ in lc.rewrite_webinf_locations(text, rules, lambda s: "x")[1]]
            for loc in lc.profile_locations(text, rules):
                line, text = self._lc_profile_location(c, boot, cp, loc, text)
                if line:
                    profile_lines.append(line)
            # CommonsMultipartResolver는 Spring 6에서 삭제됐다. 빈을 지우고 업로드 한도는 Boot 속성으로 옮긴다
            for el in reversed(lc.beans_of_class(text, rules.multipart_classes)):
                bid = el.attrs.get("id") or "multipartResolver"
                limits = lc.multipart_limits(text, el, rules)
                if LC.multipart_resolver:
                    text = lc.remove_element(text, el, f"sbmig: {el.attrs.get('class', '').rsplit('.', 1)[-1]} bean "
                                             f"{bid} removed (Spring 6 deleted the class); Spring Boot's multipart "
                                             "support uses the spring.servlet.multipart.* limits in application.properties")
                    res.beans_removed.append(f"{cp}: {bid} ({el.attrs.get('class', '').rsplit('.', 1)[-1]} → "
                                             "spring.servlet.multipart.*)")
                    multipart_rows += [(k, v, f"{el.attrs.get('class', '').rsplit('.', 1)[-1]} {bid} {prop} ({cp})")
                                       for k, v, prop in limits]
                else:
                    c.followup(f"{cp}: bean {bid} is a {el.attrs.get('class')} — Spring 6 deleted the class, startup "
                               "fails: remove the bean and set spring.servlet.multipart.max-file-size / max-request-size "
                               "(layout.legacy_context.multipart_resolver: false)")
            # JSP를 WEB-INF/jsp 아래로 옮긴 경우다. view prefix도 spring.mvc.view.prefix와 같게 바꾼다
            if c.jsp_moved:
                edits = []
                for a, b, pre in lc.view_prefixes(text, rules):
                    new_pre = self._jsp_web_path(c, pre)                         # type: ignore[attr-defined]
                    if new_pre != pre:
                        edits.append((a, b, new_pre))
                        prefix_done.append(f"{cp}: {pre} → {new_pre}")
                text = lc.rewrite_spans(text, edits)
            # <mvc:resources>의 파일은 front 모듈 static/으로 옮겨졌다. 그 위치를 덧붙인다
            for el in lc.resource_handlers(text, rules):
                line, text = self._lc_resources(c, cp, el, text)
                if line:
                    resources_lines.append(line)
            if lc.has_element(text, rules.default_servlet_elements):
                default_servlet.append(cp)
            if any(h in text for h in rules.security_xml) and lc.has_element(text, rules.security_elements):
                security.append(cp)
            if text != orig:
                c.ws.write(path, text)
                c.changed.add(path)
        dups = {i: f for i, f in ids.items() if len(f) > 1}
        res.duplicate_bean_ids = [f"{i}: {', '.join(f)}" for i, f in sorted(dups.items())]
        if jndi:
            self._lc_fact(c, "jndi_beans", jndi)
            if LC.handover_sentences:
                jdbc = self._lc_has_jdbc(c)
                for j in jndi:
                    c.followup(f"JNDI DataSource {j} has no JNDI in embedded Tomcat: remove the bean and set "
                               f"spring.datasource.* (see application.properties; spring-boot-starter-jdbc present: "
                               f"{'yes' if jdbc else 'no — add it to ' + c.boot_rel + ', spring.datasource.* binds only with spring-boot-jdbc'})")
        if res.webinf_locations_rewritten:
            c.note("/WEB-INF property locations rewritten in the legacy-config copies: "
                   + "; ".join(res.webinf_locations_rewritten[:4]))
        if webinf_left:
            c.followup("property locations under /WEB-INF are not on the Spring Boot classpath: "
                       + "; ".join(webinf_left[:4]) + f" — use classpath:{STASH}/… (layout.legacy_context."
                       "rewrite_webinf_locations: false)")
        for line in profile_lines:
            c.followup(line)
        if multipart_rows:
            written = self._lc_write_props(c, boot, "multipart limits of the as-is CommonsMultipartResolver "
                                           "(removed from the legacy-config copy)", multipart_rows)
            c.followup(f"CommonsMultipartResolver bean(s) {', '.join(b.split(': ', 1)[1].split(' (')[0] for b in res.beans_removed)} "
                       "removed from the legacy-config copies — Spring 6 deleted the class; Spring Boot's multipart support "
                       f"replaces it with {', '.join(written) or 'its defaults (1MB per file, 10MB per request)'}")
        elif res.beans_removed:
            c.followup("CommonsMultipartResolver bean(s) removed from the legacy-config copies (Spring 6 deleted the class); "
                       "Spring Boot's multipart defaults apply (1MB per file, 10MB per request) — set "
                       "spring.servlet.multipart.* to the as-is limits")
        if prefix_done:
            self._lc_fact(c, "view_prefix_rewritten", prefix_done)
            c.note("JSP view prefix rewritten in the legacy-config copies (JSPs moved under /WEB-INF/jsp/): "
                   + "; ".join(prefix_done))
        for line in resources_lines:
            c.followup(line)
        c.lc_default_servlet_xml = list(default_servlet)
        if security and LC.handover_sentences:
            self._lc_fact(c, "security_xml", security)
            c.followup(f"{', '.join(security)} configure Spring Security in XML; the recipes migrate only Java "
                       "configuration — check it against Spring Security 7 at the first start (intercept-url patterns "
                       "with ** in the middle, access=\"ROLE_X\" expressions, versioned spring-security-*.xsd, CSRF on by "
                       "default)")

    def _lc_stash(self, c: "_Ctx", boot: str, sub: str) -> Optional[str]:
        """``WEB-INF/<sub>`` 파일이 boot 모듈에 놓인 위치 ``classpath:asis-webinf/<sub>``를 반환한다.
        front 모듈로 옮길 때 그곳에 두지 않았으면 as-is 파일을 찾아 복사한다.
        파일을 찾지 못하거나 와일드카드면 None을 반환한다. 이때 위치는 고치지 않는다."""
        if "*" in sub or "?" in sub:
            return None
        dst = f"{boot}/src/main/resources/{STASH}/{sub}"
        if not c.ws.exists(dst):
            src = c.profile.source
            found = None
            for d in (src.webapp_dirs or ["src/main/webapp"]):
                cur = c.new_location(posixpath.normpath(f"{c.base + '/' if c.base else ''}{d}/WEB-INF/{sub}"))
                if c.ws.exists(cur):
                    found = cur
                    break
                cur = c.new_location(f"{d}/WEB-INF/{sub}")
                if c.ws.exists(cur):
                    found = cur
                    break
            if found is None:
                return None
            c.copy(found, dst)
        return f"classpath:{STASH}/{sub}"

    def _lc_resolvable(self, c: "_Ctx", key: str) -> bool:
        """``${key}``가 Boot에서도 해석되는지 반환한다.
        패키지에 들어가는 리소스가 정의하거나 web.xml context-param에 값이 있으면 True다."""
        inv = getattr(c.profile, "properties", None)
        for f in getattr(inv, "findings", None) or []:
            if f.key != key:
                continue
            if f.status in ("defined", "defaulted"):
                return True
            if any(r.source == "context-param" and r.default not in (None, "") for r in f.refs):
                return True
        return False

    def _lc_profile_location(self, c: "_Ctx", boot: str, cp: str, loc: str, text: str) -> tuple[str, str]:
        """``${…}``가 들어간 classpath 위치 하나를 처리하고 (인수인계 문장, 새 텍스트)를 반환한다.
        - 플레이스홀더가 Boot에서도 해석되면 그대로 둔다
        - 패키지에 들어가는 파일 하나만 맞으면 ``classpath*:`` glob으로 바꾼다
        - 여러 파일이 맞으면 고치지 않고 설정할 키를 알린다. glob으로 모두 읽으면 값이 섞인다"""
        LC = self._lc_switches(c)
        rules = self._lc_rules(c)
        keys = lc.placeholder_keys(loc)
        open_keys = [k for k, d in keys if d is None and not self._lc_resolvable(c, k)]
        names = ", ".join("${" + k + "}" for k, _ in keys)
        if not open_keys:
            return "", text
        glob = lc.glob_of(loc)
        pat = glob.split(":", 1)[1].lstrip("/")
        files = sorted({cpath for x in self._lc_classpath_files(c)
                        if (cpath := lc.classpath_rel(x)) is not None and lc.ant_match(pat, cpath)
                        and not cpath.startswith((STASH + "/", LEGACY_DIR + "/"))})
        if LC.profile_globs and len(files) == 1:
            text = lc.replace_value(text, loc, glob, rules)
            c.lc_glob_keys.update(open_keys)
            self._lc_fact(c, "profile_locations", [f"{cp}: {loc} → {glob}"])
            return (f"{cp}: {loc} chose its file by {names}, which Spring Boot does not set; only {files[0]} is packaged, "
                    f"so the copy reads {glob} — for several environments use application-<profile>.properties and "
                    "spring.profiles.active"), text
        self._lc_fact(c, "profile_locations", [f"{cp}: {loc} (left: {len(files)} file(s) match)"])
        return (f"{cp}: {loc} chooses its file by {names}; set {', '.join(open_keys)} (-D…, environment, or "
                f"{c.boot_rel}/src/main/resources/application.properties) — "
                + (f"{len(files)} packaged files match ({', '.join(files[:4])}), loading them all would mix the "
                   "environments" if len(files) > 1 else "no packaged file matches")
                + "; the Spring Boot convention is application-<profile>.properties + spring.profiles.active"), text

    @staticmethod
    def _lc_classpath_files(c: "_Ctx") -> list[str]:
        """``src/main/resources`` 아래의 properties, yml, yaml 파일 경로 목록을 반환한다."""
        out: list[str] = []
        for pat in ("*.properties", "*.yml", "*.yaml"):
            out += [r for r in c.ws.glob(pat) if "/src/main/resources/" in "/" + r]
        return out

    def _lc_resources(self, c: "_Ctx", cp: str, el: Any, text: str) -> tuple[str, str]:
        """``<mvc:resources>`` 하나를 처리해 (인수인계 문장, 새 텍스트)를 반환한다.
        웹 루트 파일은 front 모듈의 ``static/``으로 옮겨졌다.
        front 모듈에 그 디렉토리가 있으면 복사한 XML의 location에 ``classpath:/static/<loc>``를 덧붙인다.

        `rewrite_webinf_locations: false` leaves the text; the sentence then asks for the same edit."""
        LC = self._lc_switches(c)
        L = c.cfg.layout
        if not c.front_made or not c.front_rel:
            return "", text
        loc = el.attrs.get("location", "")
        parts = [x.strip() for x in loc.split(",") if x.strip()]
        web = [x for x in parts if not re.match(r"^[a-z*]+:", x) and x.startswith("/") and not x.startswith("/WEB-INF")]
        if not web:
            return "", text
        static = f"{c.p(c.front_rel)}/{L.front_static_dir.strip('/')}"
        cp_static = lc.classpath_rel(L.front_static_dir.strip("/") + "/") or ""
        if not cp_static:
            return "", text
        add = []
        for w in web:
            sub = w.strip("/")
            extra = f"classpath:/{cp_static}{sub}/"
            if c.ws.abs(f"{static}/{sub}").is_dir() and extra not in parts:
                add.append(extra)
        if not add:
            return "", text
        mapping = el.attrs.get("mapping", "")
        if LC.rewrite_webinf_locations:
            tag = text[el.start:el.tag_end]
            new_loc = ", ".join(parts + add)
            fixed = re.sub(r"""(\blocation\s*=\s*)(["']).*?\2""", lambda m: f"{m.group(1)}{m.group(2)}{new_loc}{m.group(2)}",
                           tag, count=1, flags=re.S)
            text = text[:el.start] + fixed + text[el.tag_end:]
            self._lc_fact(c, "resources_locations", [f"{cp}: {mapping} {loc} → {new_loc}"])
            return (f"<mvc:resources mapping=\"{mapping}\" location=\"{loc}\"> ({cp}) named web-root files that are "
                    f"now in {static}/{web[0].strip('/')}/ (the front jar's classpath:/{cp_static}) — sbmig added "
                    f"{', '.join(add)} to the copy's location"), text
        return (f"<mvc:resources mapping=\"{mapping}\" location=\"{loc}\"> ({cp}) names web-root files that are now in "
                f"{static}/{web[0].strip('/')}/ — add {', '.join(add)} to its location"), text

    def _lc_has_jdbc(self, c: "_Ctx") -> bool:
        """boot나 소스 모듈 pom에 spring-boot-jdbc를 가져오는 의존성이 있는지 반환한다.
        JNDI 안내 문장을 고를 때 쓴다."""
        rules = self._lc_rules(c)
        for rel in (c.boot_rel, c.comp_rel):
            if not rel:
                continue
            pom = f"{c.p(rel)}/pom.xml"
            if not c.ws.exists(pom):
                continue
            try:
                keys = {d.key for d in PomEditor(c.ws.abs(pom)).dependencies()}
            except PomEditError:
                continue
            if keys & set(rules.jdbc_artifacts):
                return True
        return False

    def _lc_legacy_properties(self, c: "_Ctx", boot: str, copies: dict[str, str]) -> None:
        """빈 덮어쓰기와 순환 참조를 허용하는 속성을 boot ``application.properties``에 쓴다.
        as-is Spring Framework와 같은 조건으로 기동하게 한다. 중복 빈 id는 인수인계에 남긴다.
        as-is가 Boot이거나 MSA인 프로젝트에는 쓰지 않는다.

        Keys: `spring.main.allow-bean-definition-overriding` / `allow-circular-references`, from the rules'
        `legacy_properties`, each behind its own switch."""
        if not self._lc_legacy_case(c):
            return
        LC = self._lc_switches(c)
        rules = self._lc_rules(c)
        rows = [(r["key"], r.get("value", "true"), r.get("why", "")) for r in rules.legacy_properties
                if getattr(LC, r.get("switch", ""), False)]
        written = self._lc_write_props(c, boot, "the as-is Spring Framework context behaviour; Spring Boot refuses "
                                       "both by default (legacy XML imported via @ImportResource)", rows) if rows else []
        dups = c.result.duplicate_bean_ids
        rel = f"{c.boot_rel}/src/main/resources/application.properties"
        if written:
            c.followup("Spring Boot refuses duplicate bean names and circular references that the as-is Spring context "
                       f"accepted; sbmig set {' and '.join(written)} in {rel} so the merged context starts as before"
                       + (f". Duplicate bean ids across the imported XML: {'; '.join(dups[:6])}" if dups else "")
                       + ". Remove the lines once they are resolved")
        else:
            c.followup("Spring Boot refuses duplicate bean names and circular references that the as-is Spring context "
                       "accepted (layout.legacy_context.allow_bean_overriding / allow_circular_references: false) — set "
                       "spring.main.allow-bean-definition-overriding / allow-circular-references if the start fails"
                       + (f"; duplicate bean ids across the imported XML: {'; '.join(dups[:6])}" if dups else ""))

    # ------------------------------------------------------------------ 프로젝트 단위 안내
    def _lc_project_sentences(self, c: "_Ctx", boot: str, comp_dirs: list[str]) -> None:
        """XML import와 관계없는 프로젝트 단위 항목의 속성과 인수인계 문장을 만든다.
        기본 서블릿, Java의 CommonsMultipartResolver와 ``@PropertySource("/WEB-INF/…")``,
        WebApplicationInitializer, log4j 1 설정 파일이 대상이다."""
        LC = self._lc_switches(c)
        if not self._lc_legacy_case(c):
            return
        rules = self._lc_rules(c)
        srcs = self._lc_sources(c, comp_dirs)
        default_servlet = list(c.lc_default_servlet_xml)
        if (default_servlet or srcs["default_servlet"]) and rules.default_servlet_property:
            where = ", ".join(default_servlet + srcs["default_servlet"])
            if LC.default_servlet:
                written = self._lc_write_props(c, boot, f"Tomcat's default servlet ({where})",
                                               [(rules.default_servlet_property, rules.default_servlet_value,
                                                 "<mvc:default-servlet-handler/> / configureDefaultServletHandling "
                                                 "forwards to it")])
                self._lc_fact(c, "default_servlet", f"{rules.default_servlet_property}={rules.default_servlet_value} "
                                                    f"({where})")
                c.followup(f"Spring Boot does not register Tomcat's default servlet; sbmig set "
                           f"{written[0] if written else rules.default_servlet_property + '=' + rules.default_servlet_value}"
                           f" for <mvc:default-servlet-handler/> in {where}")
            else:
                c.followup(f"<mvc:default-servlet-handler/> in {where} needs Tomcat's default servlet, which Spring Boot "
                           f"does not register: set {rules.default_servlet_property}={rules.default_servlet_value} "
                           "(layout.legacy_context.default_servlet: false)")
        for fqcn in srcs["multipart_resolver"] if LC.handover_sentences else []:
            c.followup(f"{fqcn} creates a CommonsMultipartResolver — Spring 6 deleted the class: delete the bean (Spring Boot "
                       "registers StandardServletMultipartResolver) and move its limits to spring.servlet.multipart.max-file-size "
                       "/ max-request-size / file-size-threshold in the boot application.properties (Boot's default is 1MB "
                       "per file)")
        for line in srcs["webinf_property_sources"]:
            if LC.handover_sentences:
                file_line, _, val = line.rpartition(": ")
                sub = val.lstrip("/")[len("WEB-INF/"):]
                c.followup(f"{file_line} reads {val}; the file now lives at classpath:{STASH}/{sub} in {c.boot_rel} — use "
                           "that location (an embedded container has no ServletContext while property sources load)")
        if not LC.handover_sentences:
            return
        for fqcn in srcs["web_initializers"]:
            self._lc_fact(c, "web_initializers", [fqcn])
            c.followup(f"{fqcn} is a WebApplicationInitializer: java -jar never calls it — move its filters / servlets / "
                       "multipart settings to WebXmlRegistrations (or @Bean registrations); on an external Tomcat it would "
                       "run next to the generated SpringBootServletInitializer and create a second context, so delete it "
                       "after porting")
        found: list[str] = []
        for d in comp_dirs:
            base = c.ws.abs(f"{d}/src/main/resources")
            if base.is_dir():
                found += sorted(c.ws.rel(f) for name in rules.log4j1_files for f in base.rglob(name) if f.is_file())
        if found:
            self._lc_fact(c, "log4j1_files", found)
            c.followup(f"log4j 1 configuration {', '.join(found[:3])} is not read: Spring Boot logs through Logback — "
                       "port it to logback-spring.xml (or logging.level.* in application.properties)")
        if srcs["enable_web_mvc"]:
            self._lc_fact(c, "enable_web_mvc", srcs["enable_web_mvc"])

    def _lc_web_xml_sentences(self, c: "_Ctx", boot: str, asis: str, plan: Any) -> None:
        """web.xml에서 나온 인수인계 문장과 정보를 만든다.
        ``<jsp-config>``, 프로필 context-param, DelegatingFilterProxy, DispatcherServlet 매핑을 다룬다."""
        LC = self._lc_switches(c)
        rules = self._lc_rules(c)
        model = self._lc_web_model(c)
        text = c.web_xml_text or ""
        if plan is not None:
            if plan.proxies:
                self._lc_fact(c, "delegating_filter_proxies", [f"{n} → {t}" for n, t in plan.proxies])
            if plan.dispatcher_mappings:
                self._lc_fact(c, "dispatcher_mappings", [f"{n}: {', '.join(p)}" for n, p in plan.dispatcher_mappings])
        if not LC.handover_sentences or model is None:
            return
        jc = self._lc_jsp_config(text)
        if jc:
            self._lc_fact(c, "jsp_config", jc)
            c.followup(f"web.xml <jsp-config> ({jc}) is not applied — the embedded Tomcat reads no web.xml; put the "
                       "page encoding / include / taglib directives into the JSPs")
        for name in rules.profile_params:
            v = (model.context_params or {}).get(name)
            if v:
                c.followup(f"web.xml context-param {name}={v} is written to {c.boot_rel}/src/main/resources/"
                           f"application.properties ({name}={v}); the as-is servers may have set it per environment — "
                           f"override it with SPRING_PROFILES_ACTIVE / -D{name}=… at start")

    @staticmethod
    def _lc_jsp_config(text: str) -> str:
        """web.xml ``<jsp-config>``를 한 줄로 요약해 반환한다. 없으면 빈 문자열을 반환한다.
        jsp-property-group 수와 주요 설정값, taglib 수를 담는다."""
        m = re.search(r"<(?:\w+:)?jsp-config\b[^>]*>(.*?)</(?:\w+:)?jsp-config>", lc._COMMENT_RE.sub("", text), re.S)
        if not m:
            return ""
        body = m.group(1)
        groups = re.findall(r"<(?:\w+:)?jsp-property-group\b[^>]*>(.*?)</(?:\w+:)?jsp-property-group>", body, re.S)
        bits: list[str] = []
        if groups:
            details: list[str] = []
            for tag in ("page-encoding", "include-prelude", "include-coda", "el-ignored", "scripting-invalid"):
                vals = sorted({v.strip() for g in groups for v in re.findall(rf"<(?:\w+:)?{tag}>\s*([^<]*?)\s*</", g)})
                if vals:
                    details.append(f"{tag} {', '.join(vals)}")
            bits.append(f"{len(groups)} jsp-property-group(s)" + (f": {'; '.join(details)}" if details else ""))
        taglibs = len(re.findall(r"<(?:\w+:)?taglib[\s>]", body))
        if taglibs:
            bits.append(f"{taglibs} taglib alias(es)")
        return "; ".join(bits)

    # ------------------------------------------------------------------ 모든 모드 공통 마무리
    def _lc_after_mode(self, c: "_Ctx", mode: str) -> None:
        """모드별 작업이 끝난 뒤 ``apply`` 끝에서 마무리 작업을 한다. 바뀐 것이 있으면 커밋한다.
        - as-is ``provided`` scope를 되돌린다
        - 소스 인코딩을 pom에 기록한다
        - as-is가 Boot인 모드면 starter 문구를 채운다
        - 저널에 ``legacy_context`` 한 줄을 남긴다"""
        before = set(c.changed)
        if mode in ("boot_pom", "msa_pom") and c.result.starter is None:
            c.result.starter = self._lc_asis_starters(c)
        try:
            self._lc_restore_scopes(c)
        except (OSError, PomEditError) as e:  # 부가 단계라 실패해도 남은 문제는 빌드가 보고한다
            log.warning("as-is scope restore skipped: %s", e)
        try:
            self._lc_source_encodings(c)
        except Exception as e:  # noqa: BLE001 - 부가 단계라 실패해도 남은 문제는 구조 검토와 빌드가 보고한다
            log.warning("source encoding step skipped: %s: %s", type(e).__name__, e)
        r = c.result
        try:
            c.ws.journal({"event": "legacy_context", "mode": mode, "starter": r.starter,
                          "web_evidence": list(r.web_evidence), "xml_imports": len(r.xml_imports),
                          "xml_skipped": len(r.xml_skipped), "legacy_properties_written": len(r.legacy_properties_written),
                          "scope_restored": len(r.scope_restored), "source_encodings_written": len(r.source_encodings_written),
                          "webinf_locations_rewritten": len(r.webinf_locations_rewritten),
                          "beans_removed": len(r.beans_removed), "duplicate_bean_ids": len(r.duplicate_bean_ids),
                          "conversion_facts": sorted(r.conversion_facts)})
        except (OSError, AttributeError) as e:  # pragma: no cover - journal unavailable
            log.debug("journal legacy_context skipped: %s", e)
        if set(c.changed) != before:
            c.commit("restructure: legacy context (as-is provided scopes / source encodings)")

    def _lc_restore_scopes(self, c: "_Ctx") -> None:
        """as-is에서 ``provided``였던 의존성이 지금 compile이나 runtime이면 ``provided``로 되돌린다.
        OpenRewrite는 javax.servlet-api provided를 jakarta.servlet-api compile로 바꾼다.
        jakarta로 바뀐 좌표는 ``scope_successors`` 표로 찾는다."""
        from .scope_restore import restore_asis_scopes, scope_note
        on = self._lc_switches(c).restore_asis_scopes
        rules = self._lc_rules(c)
        poms = [*c.profile.poms, *([c.profile.root_pom] if c.profile.root_pom else [])]
        out = restore_asis_scopes(c.ws, poms, self._project_poms(c), rules.scope_successors,   # type: ignore[attr-defined]
                                  on=on)
        for fu in out.followups:
            c.followup(fu)
        for entry in out.restored:
            if entry not in c.result.scope_restored:
                c.result.scope_restored.append(entry)
        c.changed.update(out.changed)
        if c.result.scope_restored:
            c.note(scope_note(c.result.scope_restored))

    def _lc_source_encodings(self, c: "_Ctx") -> None:
        """소스가 MS949나 EUC-KR인 모듈의 pom에 ``project.build.sourceEncoding``을 기록한다.
        pom 체인에 맞는 인코딩이 이미 있으면 건너뛴다. 구조 검토 전에 실행된다.
        UTF-8과 섞인 모듈이나 컴파일러 ``<encoding>``이 다른 모듈은 인수인계 문장만 남긴다."""
        from .source_encoding import write_source_encodings
        out = write_source_encodings(c.ws, c.cfg, on=self._lc_switches(c).write_source_encoding)
        for fu in out.followups:
            c.followup(fu)
        c.changed.update(out.changed)
        c.result.source_encodings_written.extend(out.written)
        for n in out.notes:
            c.note(n)
