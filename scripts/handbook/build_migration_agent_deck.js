// migration agent — design deck, v2.1i (2026-10-28): the current flow as slides (docs/v2/KB-AND-FLOW.md, docs/INTERFACES.md).
// Run from scripts/handbook:  NODE_PATH=$PWD/node_modules APPLY_THEME_JS=<pptx skill>/scripts/apply_theme.js node build_migration_agent_deck.js ../../docs/migration-agent/migration-agent.pptx
// Diagram PNGs come from docs/migration-agent/diagrams (v2-*.mmd rendered with @mermaid-js/mermaid-cli: npx -y -p @mermaid-js/mermaid-cli mmdc -i x.mmd -o x.png -b white -w 1400 -s 3).
const path = require("path");
const sizeOf = require("image-size");
const pptxgen = require("pptxgenjs");
let applyTheme = null;
if (process.env.APPLY_THEME_JS) { try { ({ applyTheme } = require(process.env.APPLY_THEME_JS)); } catch (e) { console.warn("apply_theme.js not loadable:", e.message); } }
const OUT = process.argv[2] || "migration-agent.pptx";
const DIAG = path.resolve(__dirname, "../../docs/migration-agent/diagrams");
const pres = new pptxgen();
pres.layout = "LAYOUT_16x9";               // 10 x 5.625 in
const THEME = { name: "sbmig Forest", headFontFace: "Cambria", bodyFontFace: "Calibri",
  colors: { dk1: "1C2A1E", lt1: "FFFFFF", dk2: "2C5F2D", lt2: "EEF3EA", accent1: "2C5F2D", accent2: "97BC62", accent3: "E07A5F",
            accent4: "3D5A80", accent5: "F2CC8F", accent6: "81B29A", hlink: "2C5F2D", folHlink: "97BC62" } };
const K = THEME.colors;
pres.theme = { headFontFace: THEME.headFontFace, bodyFontFace: THEME.bodyFontFace };
pres.author = "sbmig"; pres.title = "migration agent"; pres.subject = "JDK 21 and Spring Boot 4.1 migration agent design";
const C = pres.SchemeColor;
const FOOT = { x: 0.5, y: 5.25, w: 5, h: 0.25, fontSize: 9, color: "8A8F8A", margin: 0 };

pres.defineSlideMaster({ title: "TITLE", background: { color: K.dk2 }, objects: [
  { placeholder: { options: { name: "title", type: "title", x: 0.6, y: 1.5, w: 8.8, h: 1.5, fontSize: 38, bold: true, color: C.background1, align: "left", valign: "bottom", margin: 0 }, text: "" } },
  { placeholder: { options: { name: "body", type: "body", x: 0.6, y: 3.15, w: 8.8, h: 1.3, fontSize: 17, color: C.background2, align: "left", valign: "top", margin: 0 }, text: "" } },
]});
pres.defineSlideMaster({ title: "SECTION", background: { color: K.dk1 }, objects: [
  { placeholder: { options: { name: "title", type: "title", x: 0.6, y: 1.9, w: 8.8, h: 1.1, fontSize: 34, bold: true, color: C.background1, align: "left", valign: "bottom", margin: 0 }, text: "" } },
  { placeholder: { options: { name: "body", type: "body", x: 0.6, y: 3.1, w: 8.8, h: 1.0, fontSize: 16, color: C.background2, align: "left", valign: "top", margin: 0 }, text: "" } },
]});
pres.defineSlideMaster({ title: "CONTENT", background: { color: K.lt1 }, objects: [
  { placeholder: { options: { name: "title", type: "title", x: 0.5, y: 0.3, w: 9, h: 0.7, fontSize: 24, bold: true, color: C.text2, align: "left", valign: "middle", margin: 0 }, text: "" } },
  { text: { text: "migration agent · 설계 v2.1i · 2026-10-28", options: { ...FOOT } } },
], slideNumber: { x: 9.0, y: 5.25, w: 0.5, h: 0.25, fontSize: 9, color: "8A8F8A", align: "right" } });

// ---------------------------------------------------------------- helpers (from build_deck.js)
function content(title, section, notes) { const s = pres.addSlide({ masterName: "CONTENT", sectionTitle: section }); s.addText(title, { placeholder: "title" }); if (notes) s.addNotes(notes); return s; }
function box(s, x, y, w, h, o = {}) {
  const opts = { x, y, w, h, fill: { color: o.fill || K.lt2 }, line: { color: o.line || o.fill || K.lt2, width: 0.75 }, rectRadius: 0.08 };
  if (o.shadow) opts.shadow = { type: "outer", color: "000000", blur: 3, offset: 1, angle: 90, opacity: 0.12 };
  s.addShape(pres.shapes.ROUNDED_RECTANGLE, opts);
}
function card(s, x, y, w, h, head, body, o = {}) {
  box(s, x, y, w, h, { fill: o.fill || K.lt2, shadow: true });
  s.addText(head, { x: x + 0.15, y: y + 0.1, w: w - 0.3, h: 0.38, fontSize: o.headSize || 13, bold: true, color: o.headColor || C.text2, margin: 0, isTextBox: true, valign: "top" });
  const items = Array.isArray(body) ? body.map((t, i) => ({ text: t, options: { bullet: true, breakLine: i < body.length - 1, paraSpaceAfter: 3 } })) : body;
  s.addText(items, { x: x + 0.15, y: y + 0.5, w: w - 0.3, h: h - 0.6, fontSize: o.bodySize || 11.5, color: o.bodyColor || C.text1, margin: 0, isTextBox: true, valign: "top" });
}
function bullets(s, items, x, y, w, h, size = 14) {
  s.addText(items.map((t, i) => ({ text: t, options: { bullet: true, breakLine: i < items.length - 1, paraSpaceAfter: 6 } })),
    { x, y, w, h, fontSize: size, color: C.text1, margin: 0, isTextBox: true, valign: "top" });
}
function para(s, text, x, y, w, h, o = {}) { s.addText(text, { x, y, w, h, fontSize: o.size || 12, color: o.color || C.text1, bold: !!o.bold, italic: !!o.italic, align: o.align || "left", valign: o.valign || "top", margin: 0, isTextBox: true, fontFace: o.font }); }
function flow(s, items, x, y, w, h, o = {}) {
  const n = items.length, gap = o.gap || 0.24, bw = (w - gap * (n - 1)) / n;
  items.forEach((it, i) => {
    const bx = x + i * (bw + gap);
    box(s, bx, y, bw, h, { fill: it.fill || K.dk2 });
    s.addText(it.t, { x: bx, y: y + 0.04, w: bw, h: it.sub ? h * 0.48 : h - 0.08, fontSize: o.size || 12, bold: true, color: it.color || C.background1, align: "center", valign: "middle", margin: 0.02, isTextBox: true });
    if (it.sub) s.addText(it.sub, { x: bx + 0.04, y: y + h * 0.5, w: bw - 0.08, h: h * 0.5 - 0.06, fontSize: o.subSize || 9.5, color: it.subColor || C.background2, align: "center", valign: "top", margin: 0, isTextBox: true });
    if (i < n - 1) s.addShape(pres.shapes.RIGHT_ARROW, { x: bx + bw + 0.03, y: y + h / 2 - 0.09, w: gap - 0.06, h: 0.18, fill: { color: K.accent2 }, line: { color: K.accent2 } });
  });
}
function stat(s, x, y, w, num, label, size) {
  s.addText(num, { x, y, w, h: 0.75, fontSize: size || 38, bold: true, color: C.accent1, align: "center", margin: 0, isTextBox: true, valign: "bottom" });
  s.addText(label, { x, y: y + 0.8, w, h: 0.55, fontSize: 11.5, color: C.text1, align: "center", margin: 0, isTextBox: true, valign: "top" });
}
function table(s, rows, x, y, w, colW, o = {}) {
  const size = o.size || 10.5;
  const head = rows[0].map(t => ({ text: t, options: { bold: true, color: "FFFFFF", fill: { color: K.dk2 }, fontSize: size, valign: "middle" } }));
  const body = rows.slice(1).map((r, ri) => r.map(t => ({ text: t, options: { fontSize: size, color: K.dk1, fill: { color: ri % 2 ? "FFFFFF" : K.lt2 }, valign: "top" } })));
  s.addTable([head, ...body], { x, y, w, colW, border: { type: "solid", color: "D5DBD3", pt: 0.5 }, margin: 0.05, fontFace: THEME.bodyFontFace, autoPage: false });
}
function mono(s, text, x, y, w, h, o = {}) { s.addText(text, { x, y, w, h, fontSize: o.size || 10, fontFace: "Courier New", color: o.color || C.text1, margin: 0.08, isTextBox: true, valign: "top", fill: o.fill ? { color: o.fill } : undefined }); }

// ---------------------------------------------------------------- diagram helpers
function fitImage(s, name, x, y, w, h) {
  const file = path.join(DIAG, name + ".png");
  const d = sizeOf(file);
  const k = Math.min(w / d.width, h / d.height);
  const iw = d.width * k, ih = d.height * k;
  s.addImage({ path: file, x: x + (w - iw) / 2, y: y + (h - ih) / 2, w: iw, h: ih });
  return { iw, ih };
}
// Wide pictures (ratio ≥ 2.2) span the slide with the bullets underneath; the others sit left with bullets right.
function diagram(title, section, name, items, notes, o = {}) {
  const s = content(title, section, notes);
  const d = sizeOf(path.join(DIAG, name + ".png"));
  const ratio = d.width / d.height;
  if (ratio >= 2.2 && !o.side) {
    const bh = o.bulletH || 1.15;
    const area = 5.15 - 1.1 - bh - 0.1;
    fitImage(s, name, 0.5, 1.1, 9, area);
    bullets(s, items, 0.5, 5.15 - bh, 9, bh, o.size || 11);
  } else {
    const iw = o.imgW || 6.0;
    fitImage(s, name, 0.4, 1.1, iw, 4.05);
    bullets(s, items, 0.4 + iw + 0.2, 1.2, 9.6 - (0.4 + iw + 0.2), 3.9, o.size || 11.5);
  }
  if (o.after) o.after(s);
  return s;
}
let CUR = "";
function addSec(t) { pres.addSection({ title: t }); CUR = t; }

// ================================================================ 표지
// ================================================================ 표지
addSec("표지");
{ const s = pres.addSlide({ masterName: "TITLE", sectionTitle: "표지" });
  s.addText("migration agent", { placeholder: "title" });
  s.addText("JDK 21 · Spring Boot 4.1 마이그레이션 에이전트 — 현행 설계 (v2.1i)\n2026-10-28 · 브랜치 flow-v2 · 사내 Maven 프로젝트 약 500개를 순차 처리", { placeholder: "body" });
  s.addNotes("이 덱은 v2.1i(2026-10-28) 코드를 기준으로 합니다. 다이어그램은 docs/migration-agent/diagrams/v2-*.mmd 를 렌더링한 PNG 입니다. 설정 키·단계 이름·파일 이름은 코드 그대로 영어로 적었습니다."); }

{ const s = content("목차", "표지", "v1(핸들러·레시피 규칙·패치 재생)은 태그 company-v1 로 남아 있고, 이 덱은 v2 흐름만 다룹니다.");
  table(s, [["#", "절", "핵심 질문"],
    ["1", "개요", "무엇을, 어디까지, 어떤 규칙으로 하는가 · v1 과 무엇이 다른가"],
    ["2", "아키텍처", "패키지가 무엇을 맡고, LangGraph 가 어떻게 돌리는가"],
    ["3", "파이프라인", "접수 → 분석 → 계획 → 변환 → 재구성 → 검토 → 검증 빌드"],
    ["4", "재구성", "회사 레이아웃, 정적 리소스, JSP javax → jakarta, 구조 검토"],
    ["5", "수정 루프", "라운드, 케이스, 사다리, 채점과 되돌리기, 차단"],
    ["6", "LLM", "무엇을 주고 받나, 한도, 게이트웨이, 버전 정책, script / edits 모드"],
    ["7", "지식베이스", "errors.jsonl, 케이스 폴더, 학습 기록, 사람이 고치는 법"],
    ["8", "보고서와 운영", "Handover, SBMIG-RESULT, 플릿, CLI / 웹, 설정, 검증, 인수인계"]], 0.5, 1.15, 9, [0.7, 2.0, 6.3], { size: 11 }); }

// ================================================================ 1. 개요
addSec("개요");
{ const s = content("목적과 범위", "개요", "완료 기준은 하나: mvn clean package 가 BUILD SUCCESS. 런타임은 as-is 소유 팀, 테스트는 컴파일도 하지 않습니다(-Dmaven.test.skip=true).");
  stat(s, 0.5, 1.15, 2.1, "21", "JDK 목표 버전");
  stat(s, 2.7, 1.15, 2.1, "4.1.0", "Spring Boot 목표 버전", 34);
  stat(s, 4.9, 1.15, 2.1, "1", "완료 기준: BUILD SUCCESS");
  stat(s, 7.1, 1.15, 2.4, "0", "빌드를 막는 정책 (block 없음)");
  card(s, 0.5, 2.75, 4.4, 2.3, "범위 안", ["JDK 21 컴파일, javax → jakarta(JSP 포함), Boot 4.1 / Framework 7 API", "사내 parent(base-parent / boot-jar / comp-jar), 필수 2개, 선택 8개, 회사 모듈 구조", "최종 mvn clean package 와 Handover 보고서, 케이스별 수정 스크립트 축적"]);
  card(s, 5.1, 2.75, 4.4, 2.3, "범위 밖 (as-is 소유 팀)", ["런타임 오류와 기능 검증", "테스트 컴파일·실행 (플래그 tests-not-compiled 로 안내)", "EOL 라이브러리의 코드 수준 교체 — 권고(guide)는 안내만"], { fill: "FBEAE5" }); }

{ const s = content("v1 → v2: 무엇이 달라졌나", "개요", "현업(2026-10-26): 레시피가 무엇을 바꿀지 모르니 넣지 않는다. 이전 사내 에이전트의 두 실패 — 빌드를 안 돌리고 끝냄, 작은 LLM 의 레시피 선택 — 를 반복하지 않는 설계입니다.");
  table(s, [["", "v1 (태그 company-v1)", "v2 / v2.1 (flow-v2, 현행)"],
    ["변환", "레시피 세트 + 중간 빌드 여러 번", "OpenRewrite 1회 (Java 21 · jakarta · Boot 4) + compile 1회"],
    ["수정", "결정적 핸들러 31개 + KB 레시피 + SEARCH/REPLACE 패치", "오류 케이스마다 LLM 이 쓴 Python 스크립트(또는 edits) — KB 에 저장·재사용"],
    ["지식", "cases / recipes / ledger / patterns", "kb-v2/: errors.jsonl + cases/<id>/case.json + fix_v<N>.py"],
    ["구조 검토", "없음", "재구성 뒤 LLM 1~3회 — critical 만 edits 로 수정"],
    ["판정", "phase 마다 빌드 게이트", "같음: 모든 시도는 재빌드가 채점, 악화는 되돌림"],
    ["사람의 개입", "레시피 승격 / 거절", "libraries.yml(버전 표), 케이스 block 핀, 케이스 폴더 삭제 + reindex"]], 0.5, 1.15, 9, [1.3, 3.5, 4.2], { size: 10.5 }); }

{ const s = content("지원 케이스와 접수 규칙", "개요", "케이스 종류는 classifier 가 결정적으로 정합니다. Gradle / Ant 는 exit 3, pom 없는 트리(압축 풀린 Tomcat JSP + Java 폴더)는 회사 결정(2026-10-23)으로 거부 — sbmig inventory 가 거부 사유를 설명합니다.");
  table(s, [["case_type", "판정", "변환 레시피", "재구성"],
    ["pure_java", "Spring 없음, 서블릿 없음", "UpgradeToJava21 (+ javax 쓰면 jakarta)", "layout: {name}-boot / -back"],
    ["spring_legacy", "Boot 없는 Spring Framework 4/5", "Java 21 + JavaxMigrationToJakarta", "layout (+ front, XML 빈은 @ImportResource)"],
    ["spring_boot", "Boot 2.x / 3.x", "Java 21 + jakarta + UpgradeSpringBoot_4_0 + SpringBootProperties_4_1", "boot_pom: 디렉토리 유지, pom 만 (단일 모듈은 {name}-boot 로 감쌈)"],
    ["servlet_tomcat", "HttpServlet / web.xml / war", "Java 21 + jakarta", "layout, JSP 있으면 boot 모듈 = WAR"],
    ["msa", "배포 단위 여러 개", "서비스별 위와 같음", "msa_pom: 디렉토리 유지, pom 만"]], 0.5, 1.15, 9, [1.4, 2.2, 3.0, 2.4], { size: 10 });
  para(s, "접수에서 거부: Maven 아님(exit 3) · pom 없는 트리 · 사내 parent 해석 불가 · JDK 21 아님. 받아들임: EUC-KR / MS949 / x-windows-949 선언 pom (v2.1h), 형제 pom 만 있는 트리(aggregator 생성).", 0.5, 4.35, 9, 0.7, { size: 10.5, italic: true }); }

{ const s = content("불변조건 7가지", "개요", "CLAUDE.md 의 Invariants 와 같습니다. 7번은 실수 방지 가드이지 악의적 스크립트를 막는 샌드박스가 아닙니다.");
  table(s, [["#", "불변조건", "어디서 지키나"],
    ["1", "빌드 게이트: 실행은 반드시 최종 mvn clean package 와 미해결 목록이 있는 보고서로 끝난다", "orchestrator._step_verify · _finalize"],
    ["2", "회사 특성은 코드가 아니라 설정·지식 YAML 에", "config/*.yml, knowledge/**, <kb>/libraries.yml"],
    ["3", "지식베이스는 파일뿐 (DB 금지)", "kb-v2/ · errors/cases.py · kb/store.py"],
    ["4", "플러그인 심은 그대로: LLMRequest/Response, propose_script, edit applier, sbmig.api.migrate", "core/interfaces.py · docs/ADAPTERS.md"],
    ["5", "레시피 id 의 단일 출처 knowledge/recipes/catalog.yml (43개), BOM 3.37.0 고정", "sbmig recipes check · 스냅숏 테스트"],
    ["6", "테스트·데모는 out/ 아래에만 쓴다", "tests · scripts/e2e.sh (out/e2e/kb-*)"],
    ["7", "수정 스크립트는 sbmig.script 로만: 정적 검사, python -I 자식 프로세스, 도우미 p, 타임아웃·트리 kill", "script/runner.py · check.py · api.py"]], 0.5, 1.15, 9, [0.5, 5.5, 3.0], { size: 10 }); }

// ================================================================ 2. 아키텍처
addSec("아키텍처");
diagram("컴포넌트", "아키텍처", "v2-architecture", [
  "진입은 둘: sbmig run / fleet(CLI)와 FastAPI 웹 UI(zip 업로드, SSE 진행)",
  "LangGraph 가 intake → analyze → plan → phase×N → finalize 노드를 같은 스레드에서 돌리고, 오케스트레이터가 step 순서와 빌드 게이트를 책임집니다",
  "LLM 은 fix/(수정 스크립트·edits)와 review/(구조 검토) 두 곳에서만 호출됩니다",
  "오류 목록과 케이스 저장소는 파일(kb-v2/)이고, 플릿·웹·kb 명령이 .lock 으로 공유합니다"],
  "패키지 지도는 sbmig/__init__.py 의 한국어 docstring 과 같습니다.");

{ const s = content("패키지 지도 (읽는 순서대로)", "아키텍처", "모든 모듈·공개 함수에 한국어 docstring 이 먼저 옵니다(docs/v2/CODE-COMMENTS-KO.md). 영어 원문은 그 뒤에 그대로.");
  table(s, [["패키지", "맡는 일"],
    ["cli.py · api.py · core/", "명령·라이브러리 진입점 · 공유 계약(models, config, interfaces)"],
    ["graph/ · loop/", "LangGraph 엔진 · orchestrator(단계 순서) · fixloop(수정 루프)"],
    ["analyze/ · plan/ · rewrite/", "접수·프로필·권고 · step 목록과 레시피 카탈로그 · OpenRewrite 1회"],
    ["restructure/ · review/", "회사 레이아웃·정적 리소스·JSP jakarta · LLM 구조 검토"],
    ["build/ · triage/", "Maven 실행·로그 파서(L1/L2 서명)·저장소 버전 조회 · 케이스 묶기·순서·차단 정책"],
    ["fix/ · llm/ · script/", "프롬프트·응답 프로토콜·회사 게이트웨이 · provider 등록 · 스크립트 런타임"],
    ["errors/ · kb/ · advisory/", "오류 목록·케이스 저장소 · KB 창구와 kb 명령 · EOL 권고(안내만)"],
    ["report/ · fleet/ · web/", "보고서(Handover 먼저)·SBMIG-RESULT·한국어 문구 · 순차 일괄 실행 · 웹 UI"]], 0.5, 1.15, 9, [2.6, 6.4], { size: 10.5 }); }

diagram("LangGraph 노드 그래프", "아키텍처", "langgraph", [
  "loop.engine: graph (기본) — StateGraph 가 intake → analyze → plan → phase 반복 → finalize 를 돌립니다. inline 은 같은 보고서를 내는 단순 루프",
  "--stop-after 는 노드 사이에서만 멈춥니다; 웹의 stop 도 같은 지점",
  "웹 UI: 업로드 한 건당 작업 스레드 하나, 한 번에 한 실행(나머지는 queued, FIFO), node / journal / log / report 이벤트를 SSE 로",
  "실행마다 <run dir>/sbmig.log(DEBUG) 와 journal.jsonl 이 남습니다"],
  "stream_graph 가 웹의 진행 표시를 만듭니다. 스레드 덤프는 /api/threads.", { side: true, imgW: 5.2 });

// ================================================================ 3. 파이프라인
addSec("파이프라인");
diagram("파이프라인 전체 (v2.1)", "파이프라인", "v2-pipeline", [
  "step 이름: intake → analyze → plan → transform.rewrite → transform.compile → restructure.apply | boot_pom | pom_only → restructure.review → verify.build → (수정 루프) → 최종 빌드 → finalize",
  "빌드는 compile 1회와 verify 부터만: 변환 중간에는 빌드하지 않습니다 (현업 결정)",
  "LLM 은 restructure.review 와 수정 루프에서만 개입합니다"],
  "journal 이벤트 이름은 코드의 journal({event: …}) 그대로입니다.", { bulletH: 1.0 });

{ const s = content("접수 · 분석 · 계획", "파이프라인", "같은 프로젝트와 같은 설정이면 같은 계획이 나옵니다. sbmig analyze / sbmig plan 으로 실행 없이 볼 수 있습니다.");
  table(s, [["step", "하는 일", "산출물 · journal"],
    ["intake", "작업 사본 + git 브랜치 sbmig/<run id> 에 as-is 스냅샷 · preflight(mvn, JDK 21, 사내 parent) · Maven 만 · 형제 pom 이면 aggregator 생성 · EAR 모듈 제외", "intake · preflight · build_tool (실패 exit 3)"],
    ["analyze", "pom 그래프(EUC-KR 선언 포함) · 소스 스캔(javax 타입, JSP javax 수) · web.xml · 외부화 설정 · 케이스 종류 · 권고(advisories.yml)", "ProjectProfile · analyze · advisories"],
    ["plan", "케이스별 step 목록: 레시피 세트(카탈로그에서) · 재구성 모드 · review · verify.build", "plan.json (notes 에 이유)"]], 0.5, 1.15, 9, [1.1, 5.4, 2.5], { size: 10 }); }

{ const s = content("변환: OpenRewrite 1회 + compile 1회", "파이프라인", "레시피 BOM 3.37.0 고정(3.38.0 은 Central 에서 불일치). JSP 는 OpenRewrite 가 읽지 않으므로 재구성이 따로 바꿉니다(4장).");
  card(s, 0.5, 1.15, 4.4, 2.0, "transform.rewrite", ["UpgradeToJava21 · JavaxMigrationToJakarta(케이스가 필요할 때) · Boot: UpgradeSpringBoot_4_0 + SpringBootProperties_4_1", "mvn rewrite:run 한 번, 결과는 커밋 — 빌드하지 않음", "레시피 id 는 knowledge/recipes/catalog.yml 에서만"]);
  card(s, 5.1, 1.15, 4.4, 2.0, "transform.compile", ["mvn clean compile 한 번 — 고치지 않고 오류만 모음", "오류 목록의 첫 레코드(seen compile): 케이스 id, L1/L2 서명", "이 시점에 보이는 오류만 — 나머지는 verify 의 파도에서"], { fill: "EEF3EA" });
  card(s, 0.5, 3.3, 9, 1.75, "Maven 은 오류를 파도로 낸다 (왜 라운드가 필요한가)", ["리액터: 모듈을 의존 순서대로 하나씩, 실패하면 거기서 멈춤(fail-fast) — 뒤 모듈 오류는 앞이 통과해야 보임", "javac: 파싱 → 심볼 → 타입 검사 → 흐름 분석 단계별, 앞 단계 오류가 뒤 단계를 가림 · 기본 100개(-Xmaxerrs)까지만", "그래서 sbmig 는 라운드마다 재빌드하고, 사라진 오류는 빌드가 그 단계까지 갔을 때만 해결로 칩니다 (안 보임 ≠ 해결)"], { bodySize: 10.5 }); }

// ================================================================ 4. 재구성
addSec("재구성");
diagram("회사 레이아웃 (round 9c + v2.1g/h)", "재구성", "v2-restructure-layout", [
  "{name} = 루트 artifactId. 루트는 base-parent 상속 + aggregator, 필수 2개 선언; 선택 8개는 쓰던 것만 올림(모두 4.0.0 라인)",
  "web root 안 <resource> 디렉토리(STATIC/resources/**: mp4·xlsx·템플릿·css)는 back classpath 에 남기고 front static 에 웹 경로 그대로 복사",
  "JSP·JSPF·tag 의 javax EE 이름은 분석기 표대로 jakarta 로 (코드 영역만, 문자열·주석·taglib URI 제외, 파일 인코딩 유지)",
  "WEB-INF 의 설정 파일(web.xml, Spring XML, properties)은 boot 의 asis-webinf/ 보관함, 그 외는 front jar 의 META-INF/resources/WEB-INF/"],
  "pre-9c 레이아웃(module/boot-jar + comp-jar)은 layout.* 키로 남아 있고 로컬 e2e 가 씁니다.", { bulletH: 1.35, size: 10 });

{ const s = content("재구성 모드와 규칙", "재구성", "재구성은 결정적입니다. pom 변경은 PomEditor 가 선언된 인코딩 그대로 저장합니다(EUC-KR 계열은 cp949).");
  table(s, [["모드", "언제", "하는 일"],
    ["layout", "pure_java · spring_legacy · servlet_tomcat", "{name}-boot / -back / -front 로 분리, 역할 parent, web.xml → 등록 Bean, placeholder properties, 멀티모듈은 merge(first-wins)"],
    ["boot_pom (in-place)", "Boot as-is (이전 사내 도구로 옮긴 것 포함)", "디렉토리 유지, pom 만: root → base-parent, *-boot → boot-jar, 그 외 → comp-jar; 단일 모듈은 {name}-boot 로 감쌈"],
    ["msa_pom", "MSA", "서비스마다 같은 parent · 필수 · 선택, 구조 불변"],
    ["optional_bump", "모든 모드 안에서", "쓰던 선택 아티팩트 8개만 4.0.0 으로; 새 parent 가 관리하는 버전은 명시 버전 제거(drop_managed_versions)"]], 0.5, 1.15, 9, [1.7, 2.6, 4.7], { size: 10 });
  para(s, "모든 모드 끝에 JSP javax → jakarta 변환(layout.jsp_jakarta_rename, 기본 true) → 보고서 'What we changed' 한 줄. 끄면 Handover 항목.", 0.5, 4.5, 9, 0.5, { size: 10.5, italic: true }); }

diagram("구조 검토 (restructure.review)", "재구성", "v2-review", [
  "재구성 결과를 LLM 이 회사 규칙으로 봅니다: pom 과 의존성, 모듈 구조, 소스 인코딩, 필터링에 걸리는 바이너리 리소스",
  "critical 만 <edits> 로 고치고 커밋 review: — warning 은 보고서 'Structure review' 절",
  "호출은 최대 3회: 검토 1 + 답변 형식 재시도 1 + 적용 실패 재시도 1",
  "메이저 업그레이드가 읽혀 나오면 운영자 확인 사항을 만듭니다(6장 버전 정책)"],
  "SBMIG-RESULT 의 review=ok|issues|skipped|failed 가 이 단계의 결과입니다.");

// ================================================================ 5. 수정 루프
addSec("수정 루프");
diagram("수정 루프 한 바퀴", "수정 루프", "v2-fix-loop", [
  "라운드 1 은 실패한 verify 빌드를 재사용 — 다시 빌드하지 않음. 이후 라운드마다 mvn clean package 1회, 최대 50라운드(loop.phases.verify.max_iterations), 240분",
  "오류 → 케이스(L2 서명의 sha1 앞 10자리), 근본 원인(pom·의존성) 먼저, 라운드당 최대 8 케이스",
  "이번 라운드에 누가 그 파일이나 모듈 pom 을 건드렸으면 그 케이스는 다음 빌드까지 보류(deferral)",
  "시도마다 git 커밋 1개(fix(script:<case>:v<N>) / fix(edits:<case>#n)) — 되돌리기는 커밋 단위"],
  "loop.engine 과 무관하게 루프 내부는 fixloop.py 하나입니다.", { bulletH: 1.25, size: 10.5 });

{ const s = content("채점과 되돌리기", "수정 루프", "채점은 항상 다음 빌드가 합니다. LLM 의 자기 보고는 믿지 않습니다.");
  table(s, [["결과", "뜻", "그 다음"],
    ["build_ok", "빌드 성공", "최종 빌드 → 보고서"],
    ["improved", "대상 오류가 전부 사라지고 빌드가 그 단계까지 감", "유지 · 저장본 실적 +1 · 케이스 resolved"],
    ["partial", "일부만 사라짐, 손댄 파일에 새 오류 없음", "유지 · 같은 케이스 다음 시도"],
    ["no_change", "변화 없음", "되돌림 · 다음 사다리 단계"],
    ["regressed", "손댄 파일에 새 오류, 오류 수 × regression_ratio 초과, 또는 pom 수정 뒤 더 이른 단계 오류가 대상을 가림", "되돌림(git revert) 후 재빌드"],
    ["inconclusive", "빌드가 말해 주지 못함(타임아웃, 파싱된 오류 없음, 무관한 이른 단계 실패)", "되돌림 · KB 실적 없음 · 재빌드에서 다시 채점"]], 0.5, 1.15, 9, [1.3, 4.6, 3.1], { size: 10 });
  para(s, "되돌리기가 되살린 오류는 기준선에 넣어 '새 오류' 로 세지 않습니다. 시도 없이 사라진 오류는 같은 라운드에 그 파일·pom 을 바꾼 시도를 resolved_by 로 기록합니다(v2.1f).", 0.5, 4.55, 9, 0.6, { size: 10.5, italic: true }); }

diagram("오류가 케이스가 되기까지", "수정 루프", "v2-case-id", [
  "L1 서명은 경로·숫자만 지운 것, L2 는 클래스 이름까지 지운 더 거친 서명 — 케이스 id 는 sha1(L2) 의 앞 10자리",
  "폴더 이름이 해시인 이유: 어느 PC 에서 어떤 프로젝트를 돌려도 같은 오류는 같은 폴더로 모이고, 이름 충돌·인코딩 문제가 없습니다",
  "case.json 의 name 이 사람이 읽는 이름(첫 오류 메시지), cases.md 가 표입니다",
  "coarse 한 build / other 키는 javac 줄에서 다시 세분화합니다"],
  "docs/v2/KB-AND-FLOW.md §2 에 실물 예시가 있습니다.");

diagram("사다리: 재사용 → 보강 → 신규 → 차단", "수정 루프", "v2-ladder", [
  "저장된 스크립트가 있으면 LLM 없이 먼저 실행(reuse_versions=1; 2 이상이면 실패한 저장본의 다음 버전도)",
  "실패하면 보강: 저장본 코드와 실패 이유를 주고 더 나은 스크립트를 받음 — 새 버전으로 저장(부모·이유·실행 기록)",
  "케이스당 LLM 요청은 3회(script.max_augments_per_case, 신규 포함) — 다 쓰면 BLOCKED repeated_failure 로 Handover",
  "사람이 case.json 에 block 핀을 두면 사다리를 타지 않고 바로 차단"],
  "스크립트가 코드화하기 어려운 수정이면 LLM 이 <no_script> 와 함께 edits 만 돌려줍니다 — 이 프로젝트는 고쳐지고 저장본은 없습니다.", { bulletH: 1.3, size: 10 });

{ const s = content("차단 정책과 Handover", "수정 루프", "정책상 빌드로 보지 않는 것은 없습니다(현업). 차단은 '이 실행에서 더 시도하지 않음' 이지 '안 함' 이 아닙니다.");
  table(s, [["차단 사유", "LLM 시도", "Handover 에 적는 것"],
    ["repeated_failure", "3회 (기본)", "케이스, 마지막 분석과 해결 시도, 되돌린 커밋"],
    ["javax 라이브러리 · 호환 안 되는 외부 API · 없는 아티팩트 · 플러그인 (loop.blocked_policy)", "2회 (저장본은 그대로 재사용)", "라이브러리와 저장소 조회 결과(같은 메이저 jakarta 빌드 유무)"],
    ["사내 parent 미해결 · 인코딩 · 지원 안 하는 소스", "0회 — 바로 Handover", "원인과 담당 팀이 할 일"],
    ["budget_exhausted", "—", "loop.max_llm_calls / fleet.max_llm_calls_total 소진"],
    ["사람의 block 핀", "0회", "핀의 이유 문자열"]], 0.5, 1.15, 9, [4.2, 1.8, 3.0], { size: 10 });
  para(s, "실행 전체 한도: 라운드 50 · 실행 240분 · 프로젝트당 LLM 호출 무제한(0; 양수면 상한, -1 이면 저장본만) · 라운드당 케이스 8 · 스크립트 60초 · Maven 30분 · OpenRewrite 40분.", 0.5, 4.3, 9, 0.7, { size: 10.5, italic: true }); }

// ================================================================ 6. LLM
addSec("LLM");
diagram("LLM 수정 호출: 무엇을 주고 무엇을 받나", "LLM", "v2-llm-call", [
  "주는 것: 케이스의 오류들과 오류 파일 창, pom 발췌, 'Dependency versions'(저장소 조회 사실과 판정 줄), KB 힌트(마지막 해결), 이전 시도와 실패 이유(as-is / to-be 포함)",
  "받는 것: <analysis> <solution> <verify>(운영자 확인) <edits>(JSON) <script>+<changes> 또는 <no_script>, 파일이 더 필요하면 <need_files>(최대 2회 추가)",
  "edits 는 프로젝트 폴더 안에서만 적용, script 는 python -I 자식 프로세스에서 도우미 p 로만 파일을 만집니다(60초, 트리 kill)",
  "프롬프트 60,000자, 출력 8,000 토큰, 요청 180초, 재시도 3, 전송 실패 2연속이면 60초 쉬고 1회 더"],
  "회사 게이트웨이는 messages 만 받습니다(tools 없음). 키는 .env 에서만 읽고 로그·기록에 쓰지 않습니다.", { bulletH: 1.35, size: 10 });

{ const s = content("LLM 개입 지점과 한도", "LLM", "LLM 이 개입하지 않는 곳: intake · analyze · plan · OpenRewrite · compile · restructure · 빌드 · 채점/되돌림 · 저장본 재사용 · 보고서 · KB 적재.");
  table(s, [["개입 지점", "무엇을 주고 받나", "호출 수"],
    ["구조 검토 (restructure.review)", "모든 pom(≤ 40개, 60,000자) + 모듈 구조 + 인코딩 + 계산된 사실 → 판정 JSON + critical edits", "프로젝트당 1회 + 형식 재시도 1 + 적용 실패 재시도 1 = 최대 3"],
    ["수정 루프 — 신규 (llm-new)", "오류·파일 창·pom 발췌·Dependency versions·KB 힌트·이전 시도 → 원인 / 해결 / edits / script", "케이스당 1회 + <need_files> 최대 2회 + 형식 피드백 1회"],
    ["수정 루프 — 보강 (llm-augment)", "위 + 저장 스크립트 코드와 실행 결과", "케이스당 LLM 시도 총 3회 (신규 포함)"],
    ["운영자 가이드 (verify_guide)", "메이저 업그레이드가 읽혀 나왔는데 <verify> 가 없을 때 — 바뀐 것과 확인할 것", "그 시도당 1회 (저장본 재사용 시도는 0회)"],
    ["에스컬레이션 모델", "같은 오류 집합이 2번 반복되거나 케이스의 3번째 요청부터 llm.escalation_model", "호출 수 같음, 모델만 바뀜"]], 0.5, 1.15, 9, [2.4, 4.2, 2.4], { size: 9.5 }); }

{ const s = content("회사 게이트웨이와 응답 프로토콜", "LLM", "503 사태 뒤 사용자가 요청한 대로 가장 단순한 모양을 유지합니다: yml 프로파일 없음, LangChain 없음, httpx 노브 없음. sbmig llm-probe [--bare] 가 요청·응답을 그대로 보여 줍니다.");
  card(s, 0.5, 1.15, 4.4, 3.9, "회사 게이트웨이 (provider company, 기본)", ["requests.post(LLM_BASE_URL, headers={Authorization: Bearer LLM_API_KEY}, json={model, messages, need_origin}, timeout)", ".env 는 패키지 폴더·현재 디렉토리에서 git 루트까지 올라가며 찾음(가까운 것이 이김, 셸 변수가 최우선)", "LLM_MODEL · LLM_ESCALATION_MODEL · LLM_RECORD_DIR(요청/응답 기록, 비밀 없음)", "응답에 usage 가 없으면 보고서에 usage_missing 표시 (토큰 비교 전 확인)", "오프라인: provider fake + base_url replay:<dir> (e2e 픽스처)"], { bodySize: 10.5 });
  card(s, 5.1, 1.15, 4.4, 3.9, "스크립트 런타임 (sbmig.script)", ["LLM 의 <script> 는 def fix(p) 하나 — 정적 검사(허용 import 만) 후 python -I 자식 프로세스", "도우미 p: 프로젝트 폴더 안 읽기·쓰기·치환·pom 편집만, 밖은 거부", "timeout 60초, 넘으면 프로세스 트리 kill(Windows taskkill /T)", "바뀐 파일 40개 넘으면 경고, 결과는 ScriptRunResult 로 루프에", "플러그인 자리: edit applier fn(ws, edits, *, cfg, errors=None)"], { bodySize: 10.5, fill: "EEF3EA" }); }

diagram("의존성 버전 정책 (v2.1d): 메이저는 올리지 않는다", "LLM", "v2-version-policy", [
  "사용자 결정: 같은 메이저의 jakarta 릴리스가 있으면 그것으로. 라이브러리 지식은 사람이 <kb>/libraries.yml 에 넣고 재배포 없이 반영",
  "메이저 업그레이드는 막지 않되(빌드는 되어야 하므로) Handover 맨 앞 '운영자 확인 필요' 에 무엇을 바꿨는지 적고 SBMIG-RESULT 에 major=N",
  "저장소 조회: build.repository_url(Nexus) → settings 미러 → ~/.m2 → Central, maven-metadata + 후보 pom 6개로 jakarta ✓/javax 판정, 2회 실패면 이 실행에선 조회 중단",
  "A2 결정(이대로): 같은 메이저에 jakarta 빌드가 없으면 상위 메이저 중 가장 높은 것을 권합니다"],
  "예: SiteMesh 2.4(javax) → 2.6.0(jakarta, 같은 메이저); QLRM 1.7 → 4.2.1 은 표의 allow_major 로만.", { bulletH: 1.3, size: 10 });

diagram("fix.mode: script 와 edits (토큰 비교용 스위치, v2.1i)", "LLM", "v2-modes", [
  "현행(script): LLM 이 edits 와 재사용용 스크립트를 함께 돌려주고, 스크립트는 케이스 폴더에 저장되어 다음 프로젝트에서 LLM 없이 재사용",
  "edits: LLM 이 수정 내용만 — 프롬프트에서 스크립트 규칙·도우미 API 제거(36% 짧음), 저장·재사용·보강 없음",
  "공정성: 케이스당 LLM 요청 3회, 재빌드 채점·되돌리기·운영자 확인은 두 모드 동일. 결과 구분: SBMIG-RESULT fix=edits, 보고서 usage 블록의 fix mode 줄",
  "웹 체크박스 'LLM 직접 수정만' + 'KB 사용 안 함' = 전부 LLM 실험; CLI --fix-mode edits [--no-kb]; fleet 도 같은 옵션"],
  "비교 절차: 같은 zip 을 기본으로 1회, 두 칸 켜고 1회 → 두 report.md 의 'LLM usage and cost' 비교. 프로젝트 수가 많을수록 재사용 효과가 커집니다.", { bulletH: 1.3, size: 10 });

// ================================================================ 7. 지식베이스
addSec("지식베이스");
diagram("kb-v2/ 파일 구성", "지식베이스", "v2-kb-files", [
  "errors.jsonl: 모든 실행의 오류가 한 줄씩(시도, 해결 여부, resolved_by, as-is/to-be 조각) — 모든 집계의 원천",
  "cases/<case_id>/: case.json(케이스와 스크립트 버전들의 실적) + fix_v<N>.py + 오류별 JSON",
  "cases.md 는 사람이 읽는 표, libraries.yml 은 사람이 넣는 라이브러리 버전 표, .lock 은 프로세스 간 잠금",
  "sbmig kb reindex 가 errors.jsonl 로부터 모두 다시 세고 옛 기록(실행 기록·조각·resolved_by)을 보충합니다"],
  "DB 는 없습니다(불변조건 3). 회사가 나중에 붙일 벡터 DB·RDB 는 파생 복제본이고 파일이 원본입니다.", { side: true, imgW: 5.4, size: 10.5 });

{ const s = content("case.json 속성", "지식베이스", "docs/v2/KB-AND-FLOW.md §4 에 두 프로젝트에서 쓰인 실물 case.json 이 있습니다.");
  table(s, [["속성", "뜻"],
    ["id · l2 · name", "sha1(l2)[:10] · L2 서명 · 사람이 읽는 이름(첫 오류 메시지)"],
    ["seen · resolved · projects · runs", "본 횟수 · 해결된 횟수 · 어느 프로젝트에서 · 어느 실행에서"],
    ["scripts[].version · status", "fix_v<N>.py 번호 · proven(다른 프로젝트에서도 성공) / unverified / retired"],
    ["scripts[].successes · failures · projects", "재빌드가 improved 로 채점한 횟수 · 실패 · 성공한 프로젝트"],
    ["scripts[].parent · reason · last_failure", "어느 버전을 보강했나 · 왜 새 버전이 생겼나 · 마지막 실패 이유(v2.1e)"],
    ["scripts[].runs[]", "실행 기록: 결과, 이유, 오류 수 전후, 바뀐 파일 (script_runs 30개)"],
    ["pin", "사람이 둔 'block: <이유>' — 사다리를 타지 않고 Handover 로"],
    ["last_analysis · last_solution", "마지막 LLM 의 원인·해결 요약 — 다음 프롬프트의 KB 힌트(400자)"]], 0.5, 1.15, 9, [3.2, 5.8], { size: 10 }); }

{ const s = content("학습 기록과 사람이 KB 를 고치는 법", "지식베이스", "현업 요구(2026-10-27): 왜 실패했는지 이유도 json 에, 성공 카운트도, as-is 코드와 to-be 코드도 — 사람이 같이 개선하는 에이전트.");
  card(s, 0.5, 1.15, 4.4, 3.9, "오류 레코드가 담는 것", ["tries[]: 라운드 · 시도 번호 · source(kb-script / llm-new / llm-augment / llm-edit) · 적용 결과 · 커밋", "수정 note 마다 as-is / to-be 조각 전체(errors.snippet_lines 0 = 자르지 않음)", "resolved_by: 시도 없이 사라진 오류를 고친 같은 라운드의 시도 (build ← <case> r1 try 1)", "kb errors show / kb cases show 가 코드까지 보여 줌"], { bodySize: 10.5 });
  card(s, 5.1, 1.15, 4.4, 3.9, "사람이 하는 일", ["libraries.yml 에 라이브러리의 jakarta 버전·allow_major 를 적음 — 재배포 없이 다음 실행부터", "잘못 배운 케이스: 폴더 삭제 후 sbmig kb reindex (예: SiteMesh 3.2.1 케이스)", "고치면 안 되는 케이스: case.json 에 block 핀 (sbmig kb cases pin <id> --reason …; 모르는 id 는 --create)", "다른 PC 로 옮기기: sbmig kb export / import (merge 또는 --replace, .bak 보관)", "sbmig kb stats / validate / cases list / errors show"], { bodySize: 10.5, fill: "EEF3EA" }); }

// ================================================================ 8. 보고서와 운영
addSec("보고서와 운영");
diagram("보고서 구성과 SBMIG-RESULT", "보고서와 운영", "v2-report", [
  "Handover 가 맨 앞: 소유 팀은 그것만 읽습니다 — 운영자 확인 필요(메이저 업그레이드), 남은 JSP javax, 차단된 케이스와 제안, EOL 권고",
  "SBMIG-RESULT 한 줄은 복사·붙여넣기가 안 되는 회사 PC 에서 운영자가 받아 적는 줄 — 한 줄을 유지하고 값이 있을 때만 토큰을 덧붙입니다(review= · major= · fix=)",
  "보고서 언어 report.language: ko (회사) — 문구는 report/i18n.py 한 곳에",
  "errors.md(오류 목록), report.json, changes.patch, result.zip, journal.jsonl, sbmig.log 가 run dir 에 함께"],
  "종료 코드: 0 성공 · 2 빌드 실패/차단 · 3 접수 거부(Maven 아님, JDK 아님, parent 해석 불가). 권고와 carried-over 는 종료 코드를 바꾸지 않습니다.", { bulletH: 1.3, size: 10 });

diagram("플릿: 순차 실행과 공유 KB", "보고서와 운영", "v2-fleet", [
  "에이전트 하나가 약 500 프로젝트를 엄격히 순차로 — 파일 KB 를 공유하므로 앞 프로젝트의 스크립트가 뒤에서 LLM 없이 재사용됩니다",
  "sbmig fleet run <dir|manifest> --out … [--fix-mode script|edits] [--no-kb] [--max-llm-calls-total N]; status / resume / --retry-failed",
  "state.json 의 llm_calls·토큰·비용은 모든 시도 합산(재시도 예산), 그 외 수치는 시도마다 초기화 (v2.1h)",
  "인프라 차단기(디스크·Maven·JDK 실패 연속)와 cleanup 으로 500개를 버팁니다"],
  "summary.md 머리의 settings: fix_mode=… kb=… 로 실험 A/B 출력을 구분합니다. learning.md 는 KB 가 어떻게 자랐는지.", { bulletH: 1.25, size: 10 });

{ const s = content("CLI 와 웹 UI", "보고서와 운영", "웹 실행은 한 번에 하나(FIFO 대기). stop 은 노드 사이에서만. 토큰 없으면 루프백만 허용(::ffff:127.x 포함), LAN 은 --token.");
  table(s, [["명령", "용도"],
    ["sbmig run <dir|zip> --config … [--fix-mode] [--no-kb]", "실행 (--stop-after, --dry-run, --llm-provider, --llm-plugin, --fix-plugin)"],
    ["sbmig analyze / plan / inventory", "변경 없는 분석 / 계획 / pom 없는 트리 설명"],
    ["sbmig llm-probe [--bare]", "요청 하나로 게이트웨이 확인"],
    ["sbmig recipes check --jars · advisories check", "레시피 id 를 jar 로 검증 · 권고 파일 검증"],
    ["sbmig kb stats / cases / errors / reindex / pin / export / import", "KB 관리"],
    ["sbmig fleet run / status / resume", "순차 일괄 실행"],
    ["sbmig clean [--older-than]", "끝난 실행의 사본·업로드 정리"],
    ["sbmig serve [--token]", "웹 UI (FastAPI on uvicorn)"]], 0.5, 1.15, 5.3, [3.0, 2.3], { size: 9.5 });
  card(s, 6.0, 1.15, 3.5, 3.9, "웹 UI", ["POST /api/runs?configs=&fix_mode=&no_kb= (zip) → 202", "GET /api/runs/{id}/events: SSE node / journal / log / report", "POST /api/runs/{id}/stop · GET /api/runs/{id}/files/{name}", "GET /api/threads: 스레드 덤프", "체크박스: LLM 직접 수정만 · KB 사용 안 함 → 배지 edits · KB off", "--help 는 한국어 요약"], { bodySize: 10.5 }); }

diagram("설정 계층과 주요 키", "보고서와 운영", "v2-config-layering", [
  "config/default.yml → --config yml(회사 artifacts.yml 등, 순서대로 deep merge) → CLI/API overrides → SBMIG_* 환경 변수 → ${VAR} 확장",
  "회사 좌표: artifacts.parent / boot_parent / comp_parent / mandatory(2) / optional(8); 여기서는 자리표시자뿐",
  "layout.*: module 이름, front_static_dir, front_jsp_dir, front_copy_resource_dirs, jsp_jakarta_rename, boot_as_is",
  "loop.phases.verify.max_iterations 50 · max_llm_calls 0 · max_total_minutes 240 · script.max_augments_per_case 3 · reuse_versions 1 · timeout 60s",
  "build.skip_test_compile true · repository_url · libraries.* (lookup, max_pom_checks 6) · errors.snippet_lines 0 · fix.mode script · kb.dir kb-v2"],
  "전체 키는 config/default.yml 의 주석(한국어·영어)과 docs/INTERFACES.md 에 있습니다. 키는 .env 에만.", { bulletH: 1.55, size: 10 });

{ const s = content("로그 · journal · Windows", "보고서와 운영", "운영자가 문제를 보고할 때는 파일 대신 특정 줄을 묻습니다: SBMIG-RESULT, preflight: FAIL, cause=, 의존성 버전 조회:, 워치독 줄, 스레드 덤프 블록.");
  card(s, 0.5, 1.15, 4.4, 3.9, "어디를 보나", ["지금 단계: sbmig.log 끝, 마지막 phase_start / step_start", "왜 이 step: plan.json notes, sbmig plan", "빌드 실패 이유: journal build 의 log 경로, sbmig parse-log", "케이스가 어느 길로 갔나: 'KB 탐색:' 줄 (저장본 / 보강 / 신규 / 차단)", "성공 여부: SBMIG-RESULT 한 줄, 보고서 Handover", "멈춤: 워치독 줄, threads-*.txt, /api/threads"], { bodySize: 10.5 });
  card(s, 5.1, 1.15, 4.4, 3.9, "Windows 와 인코딩", ["git / Maven 출력은 UTF-8 로 직접 디코딩, MAVEN_OPTS 에 stdout / stderr UTF-8", "pom·JSP·소스의 EUC-KR / MS949 는 선언대로 읽고 같은 계열로 저장", ".env 는 UTF-8(BOM) / UTF-16(PowerShell >) / cp949 모두 읽음", "Maven 타임아웃 시 taskkill /PID <pid> /T /F · git core.longpaths · autocrlf=false", "경로 260자: 결과 폴더를 짧은 경로(--out D:\\sbmig-out)에"], { bodySize: 10.5 }); }

{ const s = content("인수인계: 한국어 주석과 발견 목록", "보고서와 운영", "현업 요청(2026-10-28): 다음 사람도 보면서 할 수 있게. 영어 원문은 한 글자도 바꾸지 않고 한국어를 앞에 더했습니다(1,307개 docstring 을 이전 커밋과 대조).");
  stat(s, 0.5, 1.1, 2.2, "122", "모듈 전부 한국어 docstring", 32);
  stat(s, 2.75, 1.1, 2.2, "+5,547", "한국어 줄", 32);
  stat(s, 5.0, 1.1, 2.2, "26", "코드 읽으며 찾은 의심 버그 (A1~A26)", 32);
  stat(s, 7.25, 1.1, 2.25, "25", "그중 고친 것 (A2 유지)", 32);
  table(s, [["문서", "내용"],
    ["docs/v2/CODE-COMMENTS-KO.md", "주석 규약과 용어표 — 모듈·공개 함수는 한국어 먼저, 인라인 주석은 '왜'; tests/docs/test_korean_comments.py 가 지킴"],
    ["sbmig/__init__.py", "패키지 지도 (읽는 순서)"],
    ["docs/v2/KB-AND-FLOW.md", "실행 흐름, 케이스, KB 파일 실물 샘플, 사다리와 채점, LLM 한도, 버전 정책 (사내 설명용)"],
    ["docs/v2/HANDOVER-FINDINGS-2026-10-28.md", "의심 버그·정책 질문·죽은 코드·낡은 영어 문구와 처리 결과"],
    ["docs/INTERFACES.md · docs/PLAN.md", "계약(v2 → v2.1i)과 라운드별 상태표"]], 0.5, 2.6, 9, [3.3, 5.7], { size: 10 }); }

{ const s = content("검증", "보고서와 운영", "e2e 는 JDK 21(JAVA21_HOME), Maven, Maven Central 이 필요합니다. 로컬 e2e 는 사내 좌표 대신 공개 대체물(config/artifacts.local-e2e.yml)을 쓰고 LLM 은 재생 파일로 대신합니다.");
  stat(s, 0.5, 1.1, 2.2, "2,355", "오프라인 테스트 (not integration)", 32);
  stat(s, 2.75, 1.1, 2.2, "32", "e2e 픽스처 (expected.json)", 32);
  stat(s, 5.0, 1.1, 2.2, "43", "레시피 카탈로그 id", 32);
  stat(s, 7.25, 1.1, 2.25, "3", "회사 시험에서 받은 라운드 (v2.1d~i)", 32);
  table(s, [["케이스", "픽스처"],
    ["spring_boot (14)", "boot2, boot2-cloud-legacy, boot2-fix-failures, boot2-hystrix-command, boot2-javax-lib(-optional), boot2-method-rename, boot2-qlrm-javax, boot2-rewrite-excluded, boot3, company-boot-single, legacy-role-parents, multi-module-conflict / -merge"],
    ["pure_java (5)", "enforcer-java8-werror, euckr-legacy, java6-legacy, missing-artifact, pure-java"],
    ["spring_legacy (5)", "company-spring5-web, legacy-external-config, legacy-ibatis, spring5-security-batch, spring5-xml"],
    ["servlet_tomcat (6) · msa (2)", "company-servlet-jsp, ear-sibling-poms, servlet-nonstandard-dirs(JSP → jakarta 사후 검사), servlet-tomcat, servlet-war-overlay, servlet-webxml-legacy · msa, msa-shared-lib"]], 0.5, 2.55, 9, [2.0, 7.0], { size: 9 });
  para(s, "scripts/e2e.sh → out/e2e/summary.md (29 빌드 + 의도된 BLOCKED 3: boot2-javax-lib, missing-artifact, legacy-ibatis) · 재사용 시나리오: pure-java → java6-legacy", 0.5, 4.75, 9, 0.4, { size: 10.5, italic: true }); }

{ const s = content("남은 결정과 후속", "보고서와 운영", "회사 시험(step 5)은 사용자 몫: 실제 프로젝트에서 LLM 호출·시간·성공률을 company-v1 과 비교합니다.");
  card(s, 0.5, 1.15, 4.4, 3.9, "사용자가 정할 것", ["DB/ 아래 SQL 디렉토리를 어디에 둘지 (현업과 결정)", "어느 pom 도 참조하지 않는 상위 디렉토리의 웹 콘텐츠 (web-content-left 플래그)", "§B 죽은 코드·안 읽는 설정 키 두 개(review.record, openrewrite.fail_on_dry_run_results) 정리", "A2: 같은 메이저에 jakarta 가 없을 때 가장 높은 상위 메이저를 권하는 현행 유지 여부"], { bodySize: 10.5 });
  card(s, 5.1, 1.15, 4.4, 3.9, "작은 후속", ["errors.md 시도 열의 llm-edit 표기를 edits:<case>#n 으로", "구조 검토 프롬프트도 edits 모드에서 스크립트를 요청하지 않게", "FleetState 에 fix_mode 필드 (지금은 settings: 메모)", "운영 핸드북(docs/handbook)과 상세 설계 README 는 v1 기준 — 요청 시 한 번에 정리"], { bodySize: 10.5, fill: "EEF3EA" }); }

{ const s = content("요약", "보고서와 운영", "설계의 두 축은 그대로: 결정적 계획과 빌드 게이트. 바뀐 것은 수정의 단위 — 핸들러와 레시피 대신 케이스별 스크립트가 KB 에 쌓이고, 사람이 같이 고칩니다.");
  card(s, 0.5, 1.15, 2.9, 3.9, "결정적으로", ["OpenRewrite 1회, 회사 레이아웃, 케이스 id 는 서명의 해시", "LLM 은 구조 검토와 수정 루프에서만, 한도는 전부 설정 키", "같은 입력이면 같은 계획"], { bodySize: 11.5 });
  card(s, 3.55, 1.15, 2.9, 3.9, "빌드로 판정", ["모든 시도는 다음 빌드가 채점, 악화는 되돌림", "안 보임 ≠ 해결 — 빌드가 그 단계까지 갔을 때만", "실행은 항상 clean package 로 끝남"], { bodySize: 11.5 });
  card(s, 6.6, 1.15, 2.9, 3.9, "쌓이고 넘겨주기", ["케이스마다 스크립트 버전·실적·실패 이유·as-is/to-be", "Handover 가 보고서 맨 앞, SBMIG-RESULT 한 줄", "사람이 libraries.yml · block 핀 · reindex 로 개입"], { bodySize: 11.5 }); }

(async () => { await pres.writeFile({ fileName: OUT }); if (applyTheme) await applyTheme(OUT, THEME); console.log("written", OUT); })().catch(e => { console.error(e); process.exit(1); });
