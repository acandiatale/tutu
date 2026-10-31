"""재구성이 legacy Spring 컨텍스트를 다루는 부분(v2.1x, ``Restructurer`` 가 상속하는 믹스인). 왜: Spring Framework /
servlet 프로젝트를 Boot 앱으로 바꾸면 빌드는 통과해도 기동에서 깨지는 지점(빈 덮어쓰기 금지, JNDI 없음, WEB-INF
위치, CommonsMultipartResolver, 기본 서블릿, @EnableWebMvc …)을 재구성이 직접 처리하고 사실을 ``RestructureResult`` 의
v2.1x 필드(``xml_imports`` / ``xml_skipped`` / ``legacy_properties_written`` / ``scope_restored`` /
``source_encodings_written`` / ``webinf_locations_rewritten`` / ``beans_removed`` / ``duplicate_bean_ids`` / ``starter`` /
``web_evidence`` / ``conversion_facts``)에 남긴다 — 수정 루프의 layout facts, 구조 검토, 인수인계가 읽는다. 흐름상 위치:
``_generate_application`` (진입 XML 고르기 + 사본 고치기 + D1 속성), ``_write_boot`` (starter, E-M9),
``_web_xml_registrations`` (D4 / D11 / D12), ``apply`` 끝(E-M6 scope 복원, D-M7 소스 인코딩). 스위치는
``layout.legacy_context.*`` — 끄면 인수인계 문장만. 원칙: as-is 파일은 고치지 않고 ``legacy-config/`` 사본만 고친다.

Restructure side of the legacy Spring context (v2.1x, docs/INTERFACES.md "v2.1x"): a mixin of `Restructurer`. Pure
text helpers live in `legacy_context`; the class / property tables in `knowledge/config/legacy_context_rules.yml`.


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
STASH = "asis-webinf"                       # = restructurer.WEBINF_STASH (the front move's WEB-INF config stash)
NON_LEGACY_CASES = (CaseType.SPRING_BOOT, CaseType.MSA)


class LegacyContextMixin:
    """v2.1x 처리 묶음. ``Restructurer`` 의 ``_append_props`` / ``_boot_resource_files`` / ``_jsp_web_path`` /
    ``_project_poms`` 를 쓴다.

    The v2.1x steps; relies on the Restructurer helpers `_append_props`, `_boot_resource_files`, `_jsp_web_path`,
    `_project_poms`, `_servlet_scan`."""

    # ------------------------------------------------------------------ shared
    @staticmethod
    def _lc_switches(c: "_Ctx") -> Any:
        """``layout.legacy_context`` (없으면 기본값 객체)."""
        from sbmig.core.config import LegacyContextConfig

        return getattr(c.cfg.layout, "legacy_context", None) or LegacyContextConfig()

    @staticmethod
    def _lc_rules(c: "_Ctx") -> lc.LegacyContextRules:
        """규칙 표(재구성 한 번에 한 번 읽음)."""
        if c.legacy_rules is None:
            c.legacy_rules = lc.load_rules(c.cfg.resolve_path(lc.RULES_FILE))
        return c.legacy_rules

    @staticmethod
    def _lc_fact(c: "_Ctx", key: str, value: Any) -> None:
        """``conversion_facts`` 에 한 항목(목록이면 중복 없이 이어 붙임)."""
        cf = c.result.conversion_facts
        if isinstance(value, list):
            cur = cf.setdefault(key, [])
            if isinstance(cur, list):
                cur.extend(v for v in value if v not in cur)
        else:
            cf[key] = value

    @staticmethod
    def _lc_legacy_case(c: "_Ctx") -> bool:
        """Boot 로 전환되는 legacy 프로젝트인가(spring_legacy / servlet_tomcat / pure_java — Boot as-is / MSA 아님)."""
        return c.profile.case_type not in NON_LEGACY_CASES

    def _lc_prop_configured(self, c: "_Ctx", boot: str, key: str) -> bool:
        """boot 리소스(application*.properties / yml)나 이번 실행이 이미 그 키를 정했는가."""
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
        """boot ``application.properties`` 끝에 ``# sbmig: <header>`` 블록(행 = (키, 값, 출처)). 이미 정해진 키는 건너뛴다.
        쓴 ``키=값`` 목록(``legacy_properties_written`` 에도 ``(출처)`` 와 함께).

        Append `# sbmig: <header>` + `key=value` lines (rows = (key, value, why)) to the boot application.properties;
        keys already configured are skipped. Returns the `key=value` strings written."""
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
        """첫 web.xml 의 모델(현재 위치에서 다시 읽음, 한 번만). 없으면 None."""
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

    # ------------------------------------------------------------------ comp sources (D8 / D10 / D12)
    def _lc_sources(self, c: "_Ctx", comp_dirs: list[str]) -> dict[str, list[str]]:
        """comp 소스(src/main/java) 한 번 훑기: @EnableWebMvc / WebMvcConfigurationSupport (FQCN), WebApplicationInitializer,
        configureDefaultServletHandling, ``@PropertySource("/WEB-INF/…")`` (파일:줄 위치).

        One pass over the comp sources (cached): FQCNs that switch Boot's MVC auto-configuration off, web application
        initializers, default-servlet handling, `@PropertySource` values naming `/WEB-INF/…`."""
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
                # D9 (Java): new CommonsMultipartResolver(…) in a @Bean — the fix loop's note covers it, Handover only
                if any(re.search(rf"\bnew\s+(?:[\w.]+\.)?{re.escape(n)}\s*\(", body) for n in mp_simple):
                    out["multipart_resolver"].append(fqcn)
                for m in re.finditer(r"@PropertySource\s*\(([^)]*)\)", body):
                    for v in re.findall(r'"(/?WEB-INF/[^"]+)"', m.group(1)):
                        line = body[:m.start()].count("\n") + 1
                        out["webinf_property_sources"].append(f"{c.ws.rel(f)}:{line}: {v}")
        c.lc_sources = out
        return out

    # ------------------------------------------------------------------ E-M9 starter
    def _lc_web_evidence(self, c: "_Ctx", asis_deps: Optional[list[Any]] = None) -> list[str]:
        """웹 앱이라는 근거(web.xml / spring-webmvc / servlet-api / @Controller ×N / JSP ×N / 서블릿 클래스 ×N): 분석기
        라이브러리와 as-is 의존성(``web_artifacts`` 표) 둘 다 본다."""
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
        """E-M9: spring_legacy 의 웹 starter 는 웹 근거가 있을 때만. 근거가 없으면 ``starter_by_case['pure_java']`` (설정의
        비웹 starter) + 노트 + 인수인계. ``web_evidence`` / ``starter`` 를 채운다.

        E-M9 (user decision 6): a spring_legacy project without web evidence gets the configured non-web starter
        (`starter_by_case.pure_java`) instead of the webmvc one; fills `web_evidence` / `starter`."""
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
        """Boot as-is: as-is pom 들이 선언한 Boot starter 들(글로)."""
        keys: list[str] = []
        poms = [*c.profile.poms, *([c.profile.root_pom] if c.profile.root_pom else [])]
        for p in poms:
            for d in p.dependencies or []:
                if d.group_id == "org.springframework.boot" and d.artifact_id.startswith("spring-boot-starter") \
                        and (d.scope or "") != "test" and d.artifact_id not in keys:
                    keys.append(d.artifact_id)
        return ("as-is: " + ", ".join(keys)) if keys else None

    # ------------------------------------------------------------------ D2 / D3 / D1 + the copies
    def _lc_xml_imports(self, c: "_Ctx", boot: str, comp_dirs: list[str]) -> list[str]:
        """D2 + D3 + 사본 고치기 + D1: web.xml ``contextConfigLocation`` 의 진입 XML 만 ``legacy-config/`` 에 바이트 복사해
        ``@ImportResource`` 목록으로 돌려주고, ``<import>`` 로 닿는 파일은 상대 경로를 지켜 옆에 복사(목록에는 안 넣음),
        ``src/test`` 는 빼고, 사본마다 D5–D10 을 적용, Boot 가 거부하는 빈 덮어쓰기 / 순환 참조 속성(D1)을 쓴다.
        web.xml 이 없거나 아무것도 못 풀면 예전처럼 모든 XML.

        D2 / D3 / D1 and the copy edits (docs/INTERFACES.md "v2.1x"). Returns the `@ImportResource` entries."""
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
        copies: dict[str, str] = {}           # as-is path → classpath path of its copy (legacy-config/…)
        taken: dict[str, str] = {}            # classpath path of a copy → as-is path
        roles: dict[str, str] = {}
        listed: list[str] = []
        loaded: set[str] = set()
        reached_by: dict[str, str] = {}
        for asis, role in entries:
            if asis in loaded:                # already reached by an <import> of an earlier entry
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
        """D3: 바이트 그대로 복사(EUC-KR XML 의 선언이 계속 맞도록). 같은 바이트가 이미 있으면 그대로."""
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
        """사본의 ``<import resource>`` 를 따라가며 닿는 파일도 사본 옆에 복사한다. 상대 경로(앞 ``/`` 는 Spring 이 무시)는
        사본 위치 기준 같은 상대 경로로; ``classpath:`` (파일 하나)나 ``legacy-config/`` 밖으로 나가는 경로는 사본의
        resource 문자열을 사본을 가리키게 고친다. 와일드카드 ``classpath*:`` 는 그대로(as-is 위치에서 읽힘, 노트).

        Follow the `<import resource>` graph from the copy of `asis`: every reached file is copied next to the copies
        (relative paths kept; a `classpath:` import of one file or a path leaving legacy-config/ is rewritten in the
        importing copy to point at the copy). A `classpath*:` wildcard import is left (read from the as-is files)."""
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
                    # 빈 설정으로 잡히지 않은 XML(네임스페이스 접두 등)이라도 파일이 있으면 사본 옆에 둔다 — 상대 import 가 풀리게
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
        """사본마다 D5 (JNDI 빈 위 MIGRATION-TODO), D6 (/WEB-INF 위치), D7 (${…} 위치), D9 (CommonsMultipartResolver 제거),
        D10 (JSP view prefix, <mvc:resources>), D8 / Security XML 감지, 중복 빈 id(D1)를 적용·수집하고 문장을 남긴다.

        Apply the copy edits (D5 / D6 / D7 / D9 / D10) and the detections (D8, Security XML, duplicate ids)."""
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
            # D5: JNDI 조회 빈 — 빈은 두고(소유팀 결정) 바로 위에 MIGRATION-TODO 주석
            for el in sorted(lc.elements(text, rules.jndi_elements) + lc.beans_of_class(text, rules.jndi_classes),
                             key=lambda e: e.start, reverse=True):
                bid = el.attrs.get("id") or el.attrs.get("name") or el.local
                jndi.append(f"{bid} ({cp})")
                if LC.handover_sentences:
                    text, _ = lc.insert_comment_above(text, el, rules.jndi_todo)
            # D6: /WEB-INF/… 위치 → classpath:asis-webinf/… (front 이동이 거기 둔 파일; 없으면 거기로 바이트 복사)
            if LC.rewrite_webinf_locations:
                text, done = lc.rewrite_webinf_locations(text, rules, lambda sub: self._lc_stash(c, boot, sub))
                for old, new in done:
                    res.webinf_locations_rewritten.append(f"{cp}: {old} → {new}")
            else:
                webinf_left += [f"{cp}: {old}" for old, _ in lc.rewrite_webinf_locations(text, rules, lambda s: "x")[1]]
            # D7: ${…} 가 든 classpath 위치
            for loc in lc.profile_locations(text, rules):
                line, text = self._lc_profile_location(c, boot, cp, loc, text)
                if line:
                    profile_lines.append(line)
            # D9: CommonsMultipartResolver 빈(Spring 6 에서 클래스 삭제) — 사본에서 지우고 한도는 Boot 속성으로
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
            # D10: JSP view prefix — 결정 5 로 JSP 는 WEB-INF/jsp 아래로 갔다: spring.mvc.view.prefix 와 같은 변환
            if c.jsp_moved:
                edits = []
                for a, b, pre in lc.view_prefixes(text, rules):
                    new_pre = self._jsp_web_path(c, pre)                         # type: ignore[attr-defined]
                    if new_pre != pre:
                        edits.append((a, b, new_pre))
                        prefix_done.append(f"{cp}: {pre} → {new_pre}")
                text = lc.rewrite_spans(text, edits)
            # D10: <mvc:resources location="/resources/"> — 파일은 front 의 static/ 으로 갔다
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
        """D6: ``WEB-INF/<sub>`` 의 boot stash 위치(``classpath:asis-webinf/<sub>``). front 이동이 거기 두지 않았으면
        as-is 파일을 찾아 그 자리로 바이트 복사. 못 찾으면 None(위치를 고치지 않는다).

        The boot-module stash location of `WEB-INF/<sub>`; copies the as-is file there when the front move did not.
        None when the file cannot be found (the location is left)."""
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
        """``${key}`` 가 Boot 에서도 풀리는가: 패키지된 리소스가 정의하거나 web.xml context-param 값으로 쓰이는 키."""
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
        """D7: ``${…}`` 가 든 classpath 위치 하나 → (인수인계 문장, 새 텍스트). 자리값이 Boot 에서도 풀리면(기본값이 있거나
        application.properties 에 값이 쓰이는 키) 그대로; 패키지된 파일 하나만 맞으면 ``classpath*:`` glob 으로; 여럿이면
        glob 이 전부를 읽어 값이 섞이므로 고치지 않고 설정할 키를 알린다.

        D7, one `classpath:` location with `${…}` → (Handover sentence, new text). Resolvable placeholders (default
        or a value Boot gets) stay; exactly one packaged match → the `classpath*:` glob; several → left (a glob would
        merge every profile's file) with the key to set."""
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
        """지금 트리의 classpath 리소스(``src/main/resources`` 아래 *.properties / *.yml / *.yaml) 경로들."""
        out: list[str] = []
        for pat in ("*.properties", "*.yml", "*.yaml"):
            out += [r for r in c.ws.glob(pat) if "/src/main/resources/" in "/" + r]
        return out

    def _lc_resources(self, c: "_Ctx", cp: str, el: Any, text: str) -> tuple[str, str]:
        """D10: ``<mvc:resources location="/resources/">`` — 웹 루트 위치의 파일은 front 의 ``static/`` 으로 갔다. 그 디렉토리가
        있으면 사본의 location 에 ``classpath:/static/<loc>`` 를 덧붙이고(D6 스위치) 문장 하나.

        D10: a servlet-context `<mvc:resources>` location whose files moved to the front module's `static/`."""
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
        """boot / comp pom 에 spring-boot-jdbc 를 끌어오는 의존성이 있는가(D5 문장)."""
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
        """D1 (사용자 결정 1): 전환된 legacy 프로젝트의 boot application.properties 에 빈 덮어쓰기 / 순환 참조 허용
        (as-is Framework 동작) + 중복 빈 id 를 인수인계에. Boot as-is / MSA 에는 절대 쓰지 않는다.

        D1: `spring.main.allow-bean-definition-overriding` / `allow-circular-references` for a converted legacy
        project (never Boot as-is / MSA), the duplicate ids in the Handover."""
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

    # ------------------------------------------------------------------ D12 project-level sentences
    def _lc_project_sentences(self, c: "_Ctx", boot: str, comp_dirs: list[str]) -> None:
        """D8 (XML 사본 + Java), D9 (Java @Bean), D6 (Java @PropertySource), D12: WebApplicationInitializer, log4j 1 설정
        파일 — 속성과 인수인계 문장.

        Project-level steps that need no XML import: Tomcat's default servlet (D8, XML copies + Java), a Java
        CommonsMultipartResolver (D9) and `@PropertySource("/WEB-INF/…")` (D6) sentences, web application initializers and
        log4j 1 configuration files (D12)."""
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
        """D12 (web.xml 이 있을 때): ``<jsp-config>``, ``spring.profiles.active`` context-param, DelegatingFilterProxy 사실,
        DispatcherServlet 매핑 사실.

        D12 web.xml sentences: `<jsp-config>`, the profile context-params; DelegatingFilterProxy / dispatcher facts."""
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
        """web.xml 의 ``<jsp-config>`` 요약(jsp-property-group 수와 page-encoding / include-prelude / include-coda /
        el-ignored, taglib 수). 없으면 ""."""
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

    # ------------------------------------------------------------------ E-M6 / D-M7 (every pom mode)
    def _lc_after_mode(self, c: "_Ctx", mode: str) -> None:
        """``apply`` 끝(모드가 성공한 뒤): E-M6 as-is ``provided`` 복원, D-M7 소스 인코딩, Boot as-is 의 starter 글,
        저널 ``legacy_context`` 한 줄, 바뀌었으면 커밋.

        After the mode's work (layout / boot_pom / msa_pom): restore as-is `provided` scopes (E-M6), write the source
        encodings (D-M7), the Boot as-is starter text, one journal event `legacy_context`, a commit when poms changed."""
        before = set(c.changed)
        if mode in ("boot_pom", "msa_pom") and c.result.starter is None:
            c.result.starter = self._lc_asis_starters(c)
        try:
            self._lc_restore_scopes(c)
        except (OSError, PomEditError) as e:  # an add-on: the build reports what is left
            log.warning("as-is scope restore skipped: %s", e)
        try:
            self._lc_source_encodings(c)
        except Exception as e:  # noqa: BLE001 - an add-on: the review / build report what is left
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
        """E-M6: as-is 선언이 ``provided`` 였던 의존성(또는 그 jakarta 후속 좌표, ``scope_successors``)이 지금 compile /
        runtime 이면 ``provided`` 로 되돌린다(OpenRewrite 가 javax.servlet-api provided → jakarta.servlet-api compile).

        E-M6: a dependency the as-is poms declared `provided` (or its jakarta successor) that is now compile / runtime
        gets `provided` back; `scope_restored` + a note."""
        on = self._lc_switches(c).restore_asis_scopes
        rules = self._lc_rules(c)
        poms = [*c.profile.poms, *([c.profile.root_pom] if c.profile.root_pom else [])]
        provided: dict[str, str] = {}
        elsewhere: set[str] = set()          # declared compile / runtime by another as-is pom: that pom's scope wins
        for p in poms:
            for d in p.dependencies or []:
                if not (d.group_id and d.artifact_id):
                    continue
                keys = [d.key, *([rules.scope_successors[d.key]] if d.key in rules.scope_successors else [])]
                if (d.scope or "") == "provided":
                    for k in keys:
                        provided.setdefault(k, p.path)
                elif (d.scope or "compile") in ("compile", "runtime"):
                    elsewhere.update(keys)
        provided = {k: v for k, v in provided.items() if k not in elsewhere}
        if not provided:
            return
        for rel in self._project_poms(c):                                    # type: ignore[attr-defined]
            try:
                ed = PomEditor(c.ws.abs(rel))
            except PomEditError:
                continue
            for el in ed.dependency_elements(False):
                g = ed.gav_of(el)
                scope = (g.scope or "compile").strip()
                if g.key not in provided or scope in ("provided", "test", "system", "import"):
                    continue
                if not on:
                    c.followup(f"{rel}: {g.key} is {scope} although the as-is declaration was provided "
                               "(layout.legacy_context.restore_asis_scopes: false) — the container provides it")
                    continue
                ed.set_child_text(el, "scope", "provided")
                entry = f"{g.key}: {scope} → provided (as-is scope, {rel})"
                if entry not in c.result.scope_restored:
                    c.result.scope_restored.append(entry)
            c.save(ed)
        if c.result.scope_restored:
            c.note("as-is provided scopes restored after the recipes: " + "; ".join(c.result.scope_restored))

    def _lc_source_encodings(self, c: "_Ctx") -> None:
        """D-M7: 표본 .java 가 MS949 / EUC-KR 인데 프로젝트 안 pom 사슬이 맞는 ``project.build.sourceEncoding`` 을 선언하지
        않은 모듈 → 그 모듈 pom 에 적는다(검토 전). UTF-8 과 섞인 모듈, 컴파일러 플러그인 ``<encoding>`` 이 다른 값인
        모듈은 문장만.

        D-M7: write `project.build.sourceEncoding` into the module pom whose sampled sources are MS949 / EUC-KR and
        whose in-project chain declares no matching encoding (before the review); mixed modules and a conflicting
        compiler `<encoding>` → a Handover sentence only."""
        on = self._lc_switches(c).write_source_encoding
        from sbmig.analyze.pom_parser import parse_poms
        from sbmig.review.inputs import ENCODING_PROPERTY, java_charset, source_encodings

        poms, _warn = parse_poms(c.ws)
        encs = source_encodings(c.ws, poms, getattr(c.cfg.build, "encoding_candidates", None),
                                skip_tests=bool(getattr(c.cfg.build, "skip_test_compile", True)))
        for me in encs:
            bad = me.mismatch
            if not bad:
                continue
            enc = bad[0]
            charset = java_charset(enc)
            if me.mixed:
                c.followup(f"{me.pom}: the sources mix {charset} and UTF-8 files ({me.counts}) — convert one side; "
                           "sbmig wrote no project.build.sourceEncoding")
                continue
            if not on:
                c.followup(f"{me.pom}: the sources are {charset} but no project.build.sourceEncoding says so "
                           "(layout.legacy_context.write_source_encoding: false) — set it to " + charset)
                continue
            if "maven-compiler-plugin" in (me.declared_in or ""):
                c.followup(f"{me.pom}: the sources are {charset} but {me.declared_in} says {me.declared} — set it to "
                           f"{charset}")
                continue
            try:
                ed = PomEditor(c.ws.abs(me.pom))
            except PomEditError:
                continue
            ed.set_property(ENCODING_PROPERTY, charset)
            if c.save(ed):
                n = me.counts.get(enc, 0)
                line = f"{me.pom}: {ENCODING_PROPERTY}={charset} ({n} of {me.sampled} sampled .java files {enc})"
                c.result.source_encodings_written.append(line)
                c.note(f"source encoding written: {line}")
