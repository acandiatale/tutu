// migration agent — design deck (docs/migration-agent/README.md as slides).
// Run from scripts/handbook:  NODE_PATH=$PWD/node_modules APPLY_THEME_JS=<pptx skill>/scripts/apply_theme.js node build_migration_agent_deck.js ../../docs/migration-agent/migration-agent.pptx
// Diagram PNGs come from docs/migration-agent/diagrams (rendered with @mermaid-js/mermaid-cli from the .mmd sources).
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
  { text: { text: "migration agent · 설계 · 2026-10", options: { ...FOOT } } },
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
addSec("표지");
{ const s = pres.addSlide({ masterName: "TITLE", sectionTitle: "표지" });
  s.addText("migration agent", { placeholder: "title" });
  s.addText("JDK 21 · Spring Boot 4.1 마이그레이션 에이전트 상세 설계\n2026-10-26 (round 10) · 사내 Maven 프로젝트 약 500개를 순차 처리", { placeholder: "body" });
  s.addNotes("이 덱은 docs/migration-agent/README.md의 상세 설계를 요약합니다. 다이어그램은 같은 문서의 Mermaid 원본(diagrams/*.mmd)을 렌더링한 PNG입니다."); }

{ const s = content("목차", "표지", "문서의 13개 절과 같은 순서입니다.");
  table(s, [["#", "절", "핵심 질문"],
    ["1", "개요", "무엇을, 어디까지, 어떤 규칙으로 하는가"],
    ["2", "아키텍처", "어떤 컴포넌트가 무엇을 저장하는가"],
    ["3", "파이프라인", "노드마다 입력, 출력, 이벤트, 설정, 실패 동작"],
    ["4", "LangGraph", "그래프 노드, 상태, stop-after, 웹 스트림"],
    ["5–6", "루프와 트리아지", "빌드-수정 루프, 안전장치, 결정 사다리, 핸들러"],
    ["7–8", "LLM과 Blocked", "게이트웨이 호출, 컨텍스트, blocked 분류, Handover"],
    ["9–11", "재구성 · 지식 · 플릿", "회사 레이아웃, 학습 루프, 순차 플릿"],
    ["12–13", "운영과 검증", "설정 계층, CLI, 웹, Windows, 테스트와 픽스처"]], 0.5, 1.15, 9, [0.8, 2.4, 5.8], { size: 11 }); }

// ================================================================ 1. 개요
addSec("개요");

{ const s = content("목적과 범위", "개요", "완료 기준은 하나입니다: mvn clean package가 BUILD SUCCESS. 런타임은 as-is 소유 팀이 맡고, 테스트는 회사 설정에서 컴파일조차 하지 않습니다.");
  stat(s, 0.5, 1.15, 2.1, "21", "JDK 목표 버전");
  stat(s, 2.7, 1.15, 2.1, "4.1.0", "Spring Boot 목표 버전", 34);
  stat(s, 4.9, 1.15, 2.1, "1", "완료 기준: BUILD SUCCESS");
  stat(s, 7.1, 1.15, 2.4, "0", "빌드를 막는 advisory 정책");
  card(s, 0.5, 2.75, 4.4, 2.3, "범위 안", ["JDK 21 컴파일, javax → jakarta, Boot 4.1 / Framework 7 API 전환", "사내 parent, mandatory 2개, optional 8개, 회사 모듈 구조", "최종 mvn clean package와 Handover 보고서"]);
  card(s, 5.1, 2.75, 4.4, 2.3, "범위 밖 (as-is 소유 팀)", ["런타임 오류와 기능 검증", "테스트: build.skip_test_compile: true → -Dmaven.test.skip=true, 플래그 tests-not-compiled", "EOL 라이브러리의 코드 수준 교체(guide 정책은 안내만)"], { fill: "FBEAE5" }); }

{ const s = content("지원 케이스와 인테이크 규칙", "개요", "케이스는 classifier가 결정적으로 정합니다. 같은 프로젝트와 같은 설정이면 같은 계획이 나옵니다.");
  table(s, [["case_type", "판정", "spring 레시피", "재구성"],
    ["pure_java", "Spring / 서블릿 없음", "javax EE import 있을 때 jakarta, JUnit 4→5", "layout: -boot / -back"],
    ["spring_legacy", "Spring 4/5, Boot 없음", "jakarta + Framework 7 (+ Security 6.0 → 7.0 체인) + Batch / Hibernate / Thymeleaf 레시피", "layout, XML → @ImportResource"],
    ["spring_boot (+ MSA)", "Boot 2.x / 3.x", "Boot 4.0 umbrella + 4.1 props + 4.1 버전 + Jackson 쌍 + Boot 4 잔여 레시피", "boot_pom (MSA: msa_pom)"],
    ["servlet_tomcat", "HttpServlet / web.xml / war", "ServletToJakartaAndBoot (Servlet 6 제거 API 포함)", "layout, JSP면 boot WAR"]], 0.5, 1.15, 9, [1.7, 1.9, 3.3, 2.1], { size: 10 });
  card(s, 0.5, 3.3, 9, 1.75, "인테이크", ["Maven 프로젝트만 받습니다. Gradle / Ant / 빌드 파일 없음 / pom 없는 트리는 Maven 호출 전에 거절(unsupported-build-tool:<tool>, exit 3)", "sbmig inventory <dir|zip>는 pom 없는 Tomcat webapp을 읽기만 하는 설명 도구입니다(JSP, 소스, WEB-INF/lib jar 좌표 추정)", "플릿에서 거절된 프로젝트는 skipped. Maven이 JDK 21이 아니면 빌드 전에 중단(jdk-not-21, exit 3)"], { bodySize: 11 }); }

{ const s = content("불변식 6가지", "개요", "의도된 중단(Maven 없음, Maven 프로젝트 아님, 사내 parent 해석 불가)에서는 최종 빌드를 하지 않습니다. 보고서는 항상 씁니다.");
  const inv = [["1  빌드 게이트", "phase는 BUILD SUCCESS로만 완료. 실행은 마지막 clean package와 미해결 목록으로 끝남"],
    ["2  회사 특성은 YAML", "좌표, 레이아웃, 레시피, advisory는 config/와 knowledge/에"],
    ["3  파일 기반 KB", "kb/는 json · jsonl · yml, 쓰기는 kb/.lock 안에서. 데이터베이스 없음"],
    ["4  플러그인 seam 고정", "LLMRequest / LLMResponse, FixerLike, sbmig.api.migrate(...)"],
    ["5  레시피 id 단일 출처", "knowledge/recipes/catalog.yml (50개), recipe BOM 3.37.0"],
    ["6  out/ 전용", "테스트와 데모는 out/ 아래에만 씀(e2e KB는 out/e2e/kb)"]];
  inv.forEach(([h, b], i) => { const col = i % 2, row = Math.floor(i / 2); card(s, 0.5 + col * 4.6, 1.15 + row * 1.3, 4.4, 1.15, h, b, { headSize: 12.5, bodySize: 11 }); }); }

// ================================================================ 2. 아키텍처
addSec("아키텍처");

diagram("컴포넌트", "아키텍처", "architecture", [
  "CLI, 웹 UI, Python API가 같은 Orchestrator를 만들어 실행합니다",
  "loop.engine: graph이면 LangGraph StateGraph가 step_* 메서드를 차례로 부릅니다",
  "BuildFixLoop 안에서 build · triage · fix가 돌고, fix만 회사 게이트웨이를 부릅니다",
  "triage와 harvest는 kb/를, plan과 fix는 knowledge/를 읽습니다"],
  "inline 엔진도 같은 step_* 메서드를 씁니다. 두 엔진의 보고서가 같다는 것을 테스트가 확인합니다.");

diagram("데이터 저장소", "아키텍처", "data-stores", [
  "실행 디렉터리 out/<run id>/는 작업 사본(git), journal, 로그, 프로필 / 계획, 진단 파일, 산출물을 담습니다",
  "kb/는 플릿 전체가 공유합니다: 사례, 인덱스, 레시피, ledger, symbols, 에러 케이스, 케이스 카탈로그, audit",
  "llm.record_dir에는 요청 / 응답이 키 없이 남습니다",
  "KB 쓰기는 kb/.lock 안에서 원자적이고, 비밀 값은 저장 전에 가립니다(round 10)"],
  "journal.jsonl의 마지막 줄은 항상 report입니다. 없으면 프로세스가 강제 종료된 것입니다.", { side: true, imgW: 5.4 });

{ const s = content("패키지 책임", "아키텍처", "README §2.1의 표를 줄인 것입니다.");
  table(s, [["패키지", "책임"],
    ["core", "모델, 설정, 인터페이스, 작업 공간(git), 진단"],
    ["analyze · advisory", "pom 그래프, 소스, 라이브러리, 외부화 설정, web.xml, JSP, 케이스, advisory 판정 / 검증"],
    ["plan · rewrite", "규칙 기반 계획, 레시피 카탈로그, 선제 핸들러 선택 / OpenRewrite 실행"],
    ["build", "Maven 실행, 로그 파서, 시그니처, effective pom, classpath, dependency tree"],
    ["triage", "클러스터, 핸들러 31개(+ case-replace), 반응형 레시피 규칙, blocked 분류, 결정 엔진"],
    ["fix · llm", "회사 게이트웨이, 컨텍스트 패커, 프로토콜, 패치 적용 / 플러그인 어댑터"],
    ["loop · graph", "오케스트레이터, 빌드-수정 루프, 예산, BlockedRegistry / LangGraph 엔진"],
    ["restructure · report", "회사 레이아웃, PomEditor, 템플릿, WebXmlRegistrations / 보고서, Handover, 비용"],
    ["kb · fleet · web", "파일 KB와 harvest / 순차 플릿 / FastAPI + SSE"]], 0.5, 1.15, 9, [2.2, 6.8], { size: 10.5 }); }

// ================================================================ 3. 파이프라인
addSec("파이프라인");

// 9: the whole pipeline — step / node names only, the four build-fix loops drawn as ↺ cycles, no prose (user, 2026-10-23)
{ const s = content("파이프라인 전체", "파이프라인", "스텝 이름만: intake → analyze → plan → jdk21.rewrite → jdk21.loop → spring.rewrite → spring.optional_bump → (parent_phase) → pre-emptive handlers → spring.loop → restructure.apply → restructure.loop → verify.build → verify.loop → verify.build 재실행 → finalize. 각 loop는 build → triage → fix·commit → build 반복.");
  fitImage(s, "pipeline-overview", 0.3, 1.0, 9.4, 4.25); }

diagram("노드: intake", "파이프라인", "node-intake", [
  "작업 사본을 만들고 git 브랜치 sbmig/<run id>에 as-is 스냅샷을 커밋합니다",
  "preflight가 mvn, Java 21 여부(build.require_java_major), artifacts.parent, role parents를 점검합니다",
  "Maven 불가 → mvn-not-found, Java ≠ 21 → jdk-not-21, Gradle / Ant / none → unsupported-build-tool, 모두 exit 3",
  "as-is effective dependencyManagement를 캡처합니다(실패해도 계속)"],
  "journal: intake, graph_node, preflight, build_tool, effective_pom, effective_depmgmt.");

diagram("노드: analyze", "파이프라인", "node-analyze", [
  "pom 그래프, 소스, web.xml / JSP, 외부화 설정을 읽어 ProjectProfile을 만듭니다",
  "classifier가 case_type, 신뢰도, 근거를 정합니다",
  "advisories.yml로 EOL / 비관리 라이브러리를 판정합니다",
  "pom이 없거나 Maven이 아니면 중단합니다(안전망)"],
  "journal: analyze(case_type, poms, build_tool, java, spring_boot, properties_external), advisories. sbmig analyze로 실행 없이 볼 수 있습니다.");

diagram("노드: plan", "파이프라인", "node-plan", [
  "build_plan은 규칙만 씁니다. LLM 플랜 조언(plan.advisor: llm)은 화이트리스트 레시피 추가만 가능합니다",
  "advisory recipe 레시피는 spring.rewrite 맨 앞, KB 후보는 kb.try_candidates_in_plan으로 같이 돕니다",
  "선제 핸들러 목록과 실행 공유 BuildFixLoop(BlockedRegistry, LoopBudget)을 만듭니다",
  "연속된 같은 phase 스텝을 phase group으로 묶습니다"],
  "journal: advisories_planned, plan_advice, plan(steps, pre_handlers, notes). followup: 노트는 Manual follow-ups로 갑니다.");

{ const s = content("노드 상세 1: intake · analyze · plan", "파이프라인", "표의 journal 이벤트 이름은 코드의 journal({event: ...}) 호출 그대로입니다.");
  table(s, [["노드", "출력", "journal 이벤트", "설정 키", "실패 시"],
    ["intake", "project/(git), effective/depmgmt.json", "intake, preflight, build_tool, effective_depmgmt", "build.mvn, build.java_home, build.require_java_major, artifacts.parent", "mvn 없음 / Java ≠ 21 / Gradle · Ant · none → abort exit 3"],
    ["analyze", "profile.json, advisories", "analyze, advisories", "advisories.*, externalized.*", "pom 없음 → abort. advisory 오류 → 빈 목록, 계속"],
    ["plan", "plan.json, 선제 핸들러, phase group", "advisories_planned, plan_advice, plan", "plan.parent_phase, plan.optional_bump_phase, plan.intermediate_builds, plan.advisor", "실패 경로 없음"]], 0.5, 1.15, 9, [1.0, 2.0, 2.1, 2.1, 1.8], { size: 10 }); }

diagram("노드: rewrite 스텝", "파이프라인", "node-rewrite", [
  "레시피 YAML(com.company.migrate.Run)을 합성해 rewrite-maven-plugin 6.46.1을 실행합니다",
  "OOM, 다운로드 실패, fork compile 실패, 리터럴 4.0.x는 각각 한 번 재시도합니다",
  "recipe not found → phase 나머지 건너뜀, 사내 parent 해석 불가 → exit 3",
  "그 밖의 실패는 노트로 남기고 계속합니다(다음 빌드가 실제 문제를 보여 줌)"],
  "리라이트가 parent 교체보다 먼저인 이유: OpenRewrite는 as-is 클래스패스로 타입을 붙여야 javax / Boot 2·3 타입을 매칭합니다.");

diagram("노드: 선제 핸들러", "파이프라인", "node-preemptive", [
  "항상: source-encoding, strip-utf8-bom (첫 빌드 전)",
  "JDK21 그룹: 프로필 근거의 하한 / 설정 핸들러, 첫 rewrite 전",
  "SPRING 그룹: jakarta에 의존하는 8개, spring 레시피 뒤 첫 spring 루프 직전",
  "한 실행에 한 번, 커밋 handlers: <ids>, ledger에는 기록하지 않음"],
  "SPRING 그룹을 기다리게 하는 이유: 레시피 전에 servlet-api를 jakarta로 바꾸면 레시피가 타입을 인식하지 못하고 jdk21 빌드가 깨집니다.", { imgW: 5.2 });

diagram("노드: spring.optional_bump", "파이프라인", "node-optional-bump", [
  "이미 쓰는 사내 optional 아티팩트(8개)만 버전을 올립니다",
  "artifacts.remove_when_present를 지웁니다. 이동이나 parent 변경은 없습니다",
  "첫 spring 빌드 전에 사내 라이브러리의 jakarta 빌드가 클래스패스에 올라옵니다",
  "실패는 치명적이지 않습니다(NON_FATAL_RESTRUCTURE_MODES): 후속 조치만 남깁니다"],
  "멱등이므로 나중의 재구성 스텝은 올릴 것이 없다고 보고합니다.");

diagram("노드: 재구성 스텝", "파이프라인", "node-restructure", [
  "Restructurer.apply(mode, params)가 레이아웃 / pom을 바꾸고 여러 커밋을 남깁니다",
  "drop_managed_versions가 off가 아니면 effective pom을 다시 캡처합니다",
  "실패나 예외 → 변경 폐기, restructure-failed, phase 나머지 건너뜀",
  "verify는 그대로 돌지만 success=false(exit 2)입니다"],
  "journal: restructure(mode, ok, moved, generated, mandatory_added, optional_bumped, merge_conflicts …), placeholders, web_xml_registrations, restructure_failed.");

diagram("노드: verify.build", "파이프라인", "node-verify", [
  "mvn clean package 한 번. 테스트 대체 플래그를 그대로 반영합니다",
  "실패하면 verify 수정 루프(goal package, 3 라운드)를 돌고 clean package를 다시 실행합니다",
  "repeated_failure / oscillation blocked만 풀어 주고 +2 LLM 시도와 escalation 모델을 씁니다",
  "정책 / 예산 blocked만 남았으면 루프 없이 끝납니다"],
  "journal: verify, verify_loop_start, blocked_released, verify_loop_end. 끄기: loop.phases.verify.max_iterations: 0.");

diagram("노드: finalize", "파이프라인", "node-finalize", [
  "모델별 LLM 사용량 × llm.pricing = llm_cost, success와 unresolved 계산",
  "javax API 인벤토리(dependency:tree)가 blocked 항목의 via를 채웁니다",
  "infra 플래그 → advisory 검증 → Handover → 에러 케이스 → ledger → report.md / report.json → 마지막에 patch / zip",
  "_run_guard의 finally에서 항상 돌고, 마지막 journal 줄은 report입니다"],
  "round 10: KB 기록과 보고서를 먼저 쓰고 느린 changes.patch / result.zip은 마지막에 만듭니다. infra:llm / maven / jdk / repository / disk 플래그가 플릿 차단기에 쓰입니다.");

{ const s = content("노드 상세 2: 스텝 노드", "파이프라인", "loop 스텝의 내부는 5장에서 다룹니다.");
  table(s, [["노드", "journal 이벤트", "설정 키", "실패 시"],
    ["jdk21.rewrite / spring.rewrite", "rewrite(×2), rewrite_*_retry, rewrite_unresolved_versions, rewrite_parse_problems", "openrewrite.*, loop.max_total_minutes", "recipe not found → phase skip, parent → abort, 그 밖 → 계속"],
    ["선제 핸들러", "handlers_preemptive, handler_failed", "build.source_encoding_policy, build.pom_repositories", "핸들러 변경만 폐기, 계속"],
    ["spring.optional_bump", "restructure(mode optional_bump)", "artifacts.optional, remove_when_present", "후속 조치, 계속"],
    ["*.loop", "loop_start, build, triage, decision, attempt, loop_end, phase_end, kb_credit, harvest", "loop.*, build.skip_*", "phase ok=False, 계속. parent → abort"],
    ["restructure.*", "restructure, placeholders, web_xml_registrations, restructure_failed", "layout.*, artifacts.*, externalized.*", "restructure-failed, exit 2"],
    ["verify.build", "verify, verify_loop_start / _end, blocked_released", "loop.phases.verify, loop.verify_extra_llm_attempts", "결과 = 실행 결과"],
    ["finalize", "deptree, javax_api_inventory, blocked_resolved, advisories_verified, handover, report", "report.*, llm.pricing", "단계별 경고 / 플래그, 계속"]], 0.5, 1.15, 9, [1.7, 3.2, 2.2, 1.9], { size: 9 }); }

{ const s = content("실행 결과와 종료 코드", "파이프라인", "advisory와 carried-over 고정은 종료 코드를 바꾸지 않습니다.");
  stat(s, 0.5, 1.2, 2.9, "0", "최종 clean package 성공, blocked 없음");
  stat(s, 3.55, 1.2, 2.9, "2", "blocked / unresolved, restructure-failed, dry-run, stop-after");
  stat(s, 6.6, 1.2, 2.9, "3", "중단: Maven 없음, Java ≠ 21, 비 Maven, 사내 parent 해석 불가, 내부 오류, 웹 중단");
  card(s, 0.5, 2.95, 9, 2.1, "success의 정의", ["verify 빌드가 모두 성공하고 빠진 최종 빌드 · 중단 · restructure-failed가 없을 때 참(round 10)", "blocked가 남아도 success는 참일 수 있지만 종료 코드는 2", "실행 도중 예외가 나도 결과가 없는 verify 스텝을 모두 돌리고 보고서를 씁니다(불변식 1)", "마지막 줄: SBMIG-RESULT run=… exit=… build=ok|FAIL … attn=…"], { bodySize: 11 }); }

// ================================================================ 4. LangGraph
addSec("LangGraph");
diagram("LangGraph 노드 그래프", "LangGraph", "langgraph", [
  "노드 5개(intake, analyze, plan, phase, finalize)가 각각 Orchestrator.step_* 하나를 부릅니다",
  "phase 노드는 남은 phase group이 있는 동안 자기 자신으로 돌아갑니다(MSA는 서비스마다 반복, recursion limit 1000)",
  "노드는 예외를 잡지 않습니다. _RunAborted는 그래프 밖 run guard가 처리합니다",
  "상태는 작은 값만: run_id, stop_after, dry_run, phases, phase, aborted, stopped, summary"],
  "무거운 객체(RunReport, PlanStep 묶음, BuildFixLoop)는 오케스트레이터와 노드 함수가 닫아 둔 holder에 있습니다.", { bulletH: 1.45 });

diagram("웹 UI 스트림", "LangGraph", "web-stream", [
  "업로드는 202와 함께 queued 또는 running. 웹 실행은 한 번에 하나(공유 파일 KB)",
  "worker thread가 stream_graph를 돌리고 노드마다 node / journal / log 이벤트를 SSE로 보냅니다",
  "stop은 현재 노드 뒤에서 멈춥니다. verify 전이면 최종 빌드 없이 중단",
  "서버 종료 시 SSE를 닫고 살아 있는 Maven 자식 프로세스를 종료합니다"],
  "SSE 이벤트: status, node, error, journal, log, report, ping, end. 파일: report.md, report.json, result.zip, sbmig.log, journal.jsonl, changes.patch, plan.json, profile.json.", { imgW: 5.6 });

// ================================================================ 5–6. 루프와 트리아지
addSec("루프와 트리아지");

diagram("빌드-수정 루프 한 바퀴", "루프와 트리아지", "fix-loop", [
  "모든 판정은 빌드 직후에만. 채점: build_ok · improved · partial · no_change · regressed · inconclusive",
  "클러스터: compile은 파일, resolution은 g:a, 그 밖은 모듈. 라운드당 최대 8개",
  "resolution 클러스터가 같은 모듈의 compile 클러스터를 가립니다(마스킹)",
  "시도마다 git 커밋 하나: fix(handler:id), fix(kb-patch:…), fix(llm): cluster model"],
  "phase별 goal / 라운드: jdk21 compile 3, spring test-compile 6, restructure package 6, verify package 3. 빌드는 최대 라운드 수 + 1번.", { imgW: 5.6 });

diagram("루프 안전장치", "루프와 트리아지", "loop-safety", [
  "L1 stall / L2 oscillation이 stall_level을 올리고 2 이상이면 escalation 모델을 씁니다",
  "L3 회귀는 되돌리고 바로 재빌드, L4 쓸모없는 답 2회는 repeated_failure",
  "L5 전송 실패(status= / cause= 표기만) 2회 → 60초 cool-down 1회 → llm_unavailable",
  "L6 예산, L7 / L7b 테스트 대체, L12 verify 루프, L13 partial / inconclusive, L14 같은 클러스터만 셈, L15 llm-config-error"],
  "LLM이 적용할 것을 내놓지 못하면 트리가 그대로이므로 재빌드 없이 같은 결과로 다음 라운드를 진행합니다(reround).", { imgW: 5.6 });

diagram("트리아지 사다리", "루프와 트리아지", "triage-ladder", [
  "0. BlockedRegistry(L1 + 전체 경로)에 있으면 건너뜀, 0b. 사람이 고정한 케이스 동작(block · handler · recipe)",
  "1. HANDLER → 2. KB_RECIPE(active ∪ 반응형 규칙 → 후보, runNoFork 배치) → 3. KB_PATCH 재생",
  "4. blocked 판정 X1–X6: 실패한 LLM 시도 ≥ loop.blocked_policy면 BLOCKED",
  "5. LLM_FIX(클러스터당 라운드 1회). 최종 BLOCKED: llm_unavailable / repeated_failure / budget_exhausted"],
  "결정적 선택지가 아무것도 바꾸지 못하면 시도함으로 표시하고 같은 클러스터를 다음 단계로 넘깁니다(최대 6번 재결정).", { imgW: 4.6 });

{ const s = content("결정적 핸들러 31개", "루프와 트리아지", "HandlerRegistry.default().ids() 순서가 반응형 매칭 우선순위입니다. 선제 기준과 목표 버전은 다릅니다(예: lombok 1.18.30 미만이면 1.18.38 이상으로).");
  table(s, [["묶음", "핸들러", "선제"],
    ["인코딩", "source-encoding, strip-utf8-bom", "항상"],
    ["빌드 규칙 / 버전", "carry-over-managed-version, enforcer-relax, remove-werror, remove-tools-jar, systempath-relocate, missing-dependency-version", "tools-jar JDK21, 나머지 반응형"],
    ["레시피 빈틈", "jakarta-namespace-sweep, spring-data-crud-repository, jdk-base64-shim, boot4-type-relocation-sweep, security-path-pattern-matcher, spring-data-specification-unrestricted", "SPRING / base64 JDK21 / 반응형"],
    ["좌표 / jakarta", "mysql-connector-relocation, javax-servlet-api-to-jakarta, jstl-to-jakarta, dependency-relocation, jakarta-library-floor, querydsl-jakarta-classifier", "mysql JDK21, 나머지 SPRING"],
    ["누락 의존성", "jaxb-runtime-add, add-known-dependency", "JDK21 / SPRING"],
    ["프로세서 / 플러그인", "lombok-version-floor, mapstruct-version-floor, maven-compiler-plugin-floor, compiler-legacy-config, surefire-plugin-floor, remove-classic-loader-implementation", "JDK21"],
    ["리소스 / 저장소", "resources-binary-filtering, strip-pom-repositories (pom_repositories: remove)", "JDK21"],
    ["자리표시", "test-compile-skip (L7은 루프가 직접)", "없음"]], 0.5, 1.15, 9, [1.6, 5.6, 1.8], { size: 9.5 }); }

{ const s = content("jakarta 처리와 의존성 보강", "루프와 트리아지", "표 파일: jakarta_packages.yml(30), jakarta_libraries.yml(44), relocations.yml(45), known_packages.yml(75).");
  table(s, [["처리", "언제", "무엇을"],
    ["JavaxMigrationToJakarta", "spring.rewrite, 반응형 규칙(spring / restructure)", "타입이 붙은 javax EE 참조"],
    ["jakarta-namespace-sweep", "선제(javax EE import / JSP), 반응형", "레시피가 남긴 Java와 JSP의 javax.* EE 참조. 플래그 jakarta-leftovers-renamed, jsp-javax-renamed"],
    ["spring-data-crud-repository", "선제(Spring Data), 반응형(findAll() 등)", "Paging 리포지토리에 ListCrudRepository<T, ID>"],
    ["jakarta-library-floor", "선제(표의 라이브러리가 jakarta 이전 버전), 반응형(X1 via_direct)", "표 버전으로 상향, 옛 좌표 이전, classifier"],
    ["dependency-relocation", "선제(pom의 javax API 행), 반응형", "javax → jakarta API, hibernate → org.hibernate.orm, Boot 4 starter"],
    ["add-known-dependency", "반응형(package X does not exist 등)", "알려진 패키지의 아티팩트 추가. JUnit 4 잔재 → junit:junit test"]], 0.5, 1.15, 9, [2.2, 3.0, 3.8], { size: 9.5 }); }

// ================================================================ 7–8. LLM과 Blocked
addSec("LLM과 Blocked");

diagram("LLM 수정 호출", "LLM과 Blocked", "llm-fix", [
  "ContextPacker가 60,000자 안에 섹션을 묶습니다",
  "need_files / need_grep 라운드는 최대 2번, SEARCH 불일치 피드백은 1번",
  "적용은 원자적, 인코딩 보존, 30% 축소 방어",
  "판정은 다음 빌드의 _score. improved / build_ok만 harvest"],
  "시스템 프롬프트: 최소한의 기계적 수정, 동작 유지, 새 서드파티 의존성 금지, 루트 parent / 버전 다운그레이드 금지, 보지 않은 코드는 요청. 끝났는지는 오케스트레이터가 빌드로 정합니다.", { imgW: 5.8 });

diagram("컨텍스트 패킹", "LLM과 Blocked", "context-packing", [
  "Situation과 Errors는 줄이지 않습니다",
  "Source는 400줄 이하면 전체, 아니면 오류 줄 ±60줄",
  "Migration note는 키워드 점수 상위 4개, fix-policy.md는 항상",
  "예산 초과 시 notes부터 files 순서로 줄이고, 줄인 이름을 프롬프트 끝과 로그에 적습니다"],
  "TRUNCATE_ORDER = notes, plan_notes, other_errors, previous_attempts, kb_hints, reference_files, advisory_hints, reference_notes, pom, related_files, files.", { imgW: 4.8 });

{ const s = content("회사 게이트웨이와 응답 프로토콜", "LLM과 Blocked", "키는 환경(.env 또는 셸)에서만 읽고 로그와 기록에 남기지 않습니다.");
  table(s, [["항목", "값"],
    ["요청", "POST LLM_BASE_URL, Bearer LLM_API_KEY, {model, messages, need_origin}"],
    ["생성 파라미터", "보내지 않음(게이트웨이 소유)"],
    ["재시도", "408 / 409 / 425 / 429 / 5xx 계열, max_retries 3, Retry-After"],
    ["heartbeat / deadline", "30초마다 in-flight 줄 / timeout + 30초 넘으면 전송 실패"],
    ["content filter", "요청 거절로 분류(장애로 세지 않음)"],
    ["round 10", "status= / cause= 표기, 빈 답 + finish_reason=length는 요청 거절, 같은 400 두 번 → llm-config-error"],
    ["플러그인", "llm.plugin(tier 1), fix.plugin(tier 2, FixerLike)"]], 0.5, 1.15, 5.0, [1.5, 3.5], { size: 9.5 });
  mono(s, "<analysis>…</analysis>\n<confidence>0.0–1.0</confidence>\n<patches>\n=== MODIFY path/File.java\n<<<<<<< SEARCH\n기존 텍스트 (정확히 한 곳)\n=======\n바꿀 텍스트\n>>>>>>> REPLACE\n=== CREATE / DELETE / RENAME\n</patches>\n또는 <need_files> / <need_grep>", 5.7, 1.15, 3.8, 3.9, { size: 10, fill: K.lt2 }); }

diagram("blocked 분류", "LLM과 Blocked", "blocked-classify", [
  "X1 javax API(0), X2 외부 API 변경(1), X3 아티팩트 없음(0), X4 parent(0), X5 플러그인(1), X6 지원 안 하는 소스(0) — 괄호는 허용 LLM 시도",
  "cannot find symbol, package javax.x does not exist, 프로젝트 소유 클래스는 X1이 아닙니다",
  "BlockedRegistry 키는 L1 + 전체 경로, 재구성 이동은 git rename으로 따라갑니다(MSA 서비스끼리 공유 안 함)",
  "루프 사유: repeated_failure, budget_exhausted, llm_unavailable, oscillation"],
  "X7(advisory 레시피가 라이브러리 제거), X8(다운로드 실패), X9(pom 저장소 접속 불가)는 blocked가 아닌 별도 경로로 처리합니다.", { bulletH: 1.3 });

diagram("X1 원인 추적과 제안 사다리", "LLM과 Blocked", "blocked-x1", [
  "owner class → jar → GAV(dependency:build-classpath). jar가 API jar 자체면 finalize의 dependency:tree가 via를 찾습니다",
  "via_direct가 jakarta_libraries.yml에 있으면 표 버전으로(jakarta-library-floor)",
  "사내 그룹이면 artifacts.optional에 Jakarta 빌드 추가, API jar 직접 선언이면 제거",
  "경로가 없으면 그 모듈에서 dependency:tree -Dincludes를 돌리라고 안내"],
  "결과: report.json javax_api_jars, 플래그 javax-api-on-classpath, ledger 노트 via: a → b → c.", { imgW: 5.6 });

diagram("보고서와 Handover", "LLM과 Blocked", "report-handover", [
  "Handover가 맨 앞: Versions replaced(⚠ carried over), Libraries, Merge conflicts, Properties to set, From here on",
  "그다음 Blocked, javax API jars, Unresolved, Manual follow-ups, Flags",
  "기록 절: What we changed, Phases, Fix attempts, Escalated, LLM usage and cost, Knowledge base",
  "report.language: ko면 report.md만 한국어(식별자와 Maven 메시지는 영어)"],
  "carried over: 옛 parent가 관리하던 버전을 새 parent가 관리하지 않으면 as-is effective 버전으로 고정하고 ⚠로 표시합니다.", { bulletH: 1.3 });

// ================================================================ 9–11. 재구성 · 지식 · 플릿
addSec("재구성 · 지식 · 플릿");

diagram("재구성 모드 선택", "재구성 · 지식 · 플릿", "restructure-mode", [
  "MSA → msa_pom(서비스 루트 pom만), 소스 없음 → pom_only",
  "spring_boot + boot_as_is: in-place → boot_pom(디렉터리 유지, 단일 모듈은 {name}-boot로 감쌈)",
  "그 밖 → layout(boot / back / front)",
  "as-is 멀티 모듈은 merge: 모든 모듈 → {name}-back, first-wins, merge-conflicts/"],
  "모든 모드는 optional 상향과 remove_when_present를 모든 pom에 적용합니다. 이미 재구성된 프로젝트는 이동 없이 parent 버전만 정렬합니다.", { imgW: 4.6 });

diagram("회사 레이아웃", "재구성 · 지식 · 플릿", "company-layout", [
  "루트: packaging pom, parent = base-parent, mandatory 2개. 모듈은 루트 바로 아래",
  "{name}-boot(boot-jar): Application(@ServletComponentScan, @ImportResource), WebXmlRegistrations, application.properties(__SET_ME__, view prefix / suffix)",
  "{name}-back(comp-jar): 모든 소스, {name}-front(comp-jar): static과 META-INF/resources/WEB-INF/jsp",
  "JSP가 있으면 boot는 외장 Tomcat용 WAR, 아니면 실행 jar"],
  "WebXmlRegistrations는 web.xml의 서블릿 / 필터 / 리스너 / 오류 페이지를 Boot 4.1 RegistrationBean으로 옮깁니다(W1, 플래그 web-xml-registered). Boot가 직접 등록하는 클래스 14개는 skip 표로 제외합니다.", { bulletH: 1.45 });

diagram("지식 표와 쓰는 곳", "재구성 · 지식 · 플릿", "knowledge-usage", [
  "레시피 카탈로그 50, 반응형 규칙 5, advisory 67(guide 60 · recipe 7), 라이브러리 레시피 3",
  "핸들러 표: jakarta 라이브러리 44, 재배치 45, 버전 51 + 플러그인 3, 패키지 75 / 30, Boot 4 타입 25",
  "web.xml skip 14, 외부화 규칙, jar 이름 91, 노트 16(## fix-loop: 절은 advisory 힌트)",
  "회사 추가: advisories.extra_files, jar_names.yml 행, recipes/base + catalog.yml"],
  "모두 YAML / Markdown입니다(불변식 2). 코드에는 회사 값이 없습니다.", { bulletH: 1.3 });

diagram("KB 학습 루프", "재구성 · 지식 · 플릿", "kb-learning", [
  "다음 빌드에서 확인된 LLM 수정만 사례가 됩니다",
  "기계적 라벨은 LLM 없이 레시피 후보로 컴파일됩니다(ChangeType, ChangeMethodName …)",
  "후보는 다음 프로젝트의 spring.rewrite에서 실행되고 빌드 결과로 점수를 받습니다",
  "서로 다른 2개 프로젝트에서 성공하면 active → 이후 LLM 0회"],
  "증거 없는 결과(빌드 없는 phase, 해석 실패만 남은 phase, partial / inconclusive 시도)는 점수에 반영하지 않습니다. sbmig kb learning이 레시피로 만들 심볼과 여러 프로젝트를 막는 아티팩트를 보여 줍니다.", { imgW: 5.0 });

diagram("플릿: 순차 실행", "재구성 · 지식 · 플릿", "fleet", [
  "약 500개 프로젝트를 한 에이전트가 엄격히 순차로 처리합니다. KB는 항상 공유(kb/.lock)",
  "preflight(disk · maven · java · settings · repository · llm), 프로젝트마다 여유 공간 확인과 fleet.cleanup",
  "infra:<kind>로 끝난 프로젝트는 pending으로 되돌림. 연속 3개면 15분 대기 후 재점검, 안 되면 중단(exit 3)",
  "summary.csv(BOM)의 attention 열, fleet status --attention, 프로젝트마다 SBMIG-RESULT 한 줄"],
  "state.json은 입력 경로를 키로 매 프로젝트 뒤 원자적으로 갱신하고, resume은 success를 건너뜁니다. learning.md는 레시피로 만들 심볼과 여러 프로젝트를 막는 아티팩트를 보여 줍니다. intake 거절 프로젝트는 skipped. 종료 코드 0 / 2 / 3.", { imgW: 5.6 });

// ================================================================ 12–13. 운영과 검증
addSec("운영과 검증");

diagram("설정 계층", "운영과 검증", "config-layering", [
  ".env → default.yml → --config yml(순서대로) → CLI / API overrides → SBMIG_* → ${VAR:-default} 확장",
  "셸에 이미 있는 변수가 .env보다 우선합니다",
  "LLM 키는 환경에서만 읽습니다. yml에 쓰지 않습니다",
  "회사 설정: plan.parent_phase: spring, build.skip_test_compile: true, report.language: ko"],
  "config/artifacts.example.yml은 사내 좌표 자리표시자입니다. 실제 좌표는 회사에서 채웁니다.", { bulletH: 1.6 });

{ const s = content("주요 설정 키", "운영과 검증", "전체 키는 README §12.2와 핸드북 08 설정 레퍼런스에 있습니다.");
  table(s, [["키", "기본값", "의미"],
    ["plan.parent_phase", "restructure (회사 spring)", "parent / 레이아웃 적용 시점"],
    ["loop.phases.*", "3 / 6 / 6 / 3 라운드", "phase별 goal과 수정 라운드"],
    ["loop.max_llm_calls · max_total_minutes", "60 · 180", "실행 예산"],
    ["loop.stall_limit · verify_extra_llm_attempts", "2 · 2", "escalation 시작, verify 추가 시도"],
    ["build.skip_tests · skip_test_compile", "true · false (회사 true)", "테스트 실행 / 컴파일 생략"],
    ["build.settings_file · pom_repositories", "null · keep", "Nexus 미러, pom 저장소 제거"],
    ["build.require_java_major · retry_negative_cache", "21 · true", "JDK 점검, negative cache면 -U 재시도"],
    ["fleet.preflight · infra_* · cleanup · min_free_gb", "true · 3/15/4 · build-output · 5", "플릿 인프라 차단기, 디스크"],
    ["llm.model · escalation_model · recipe_model", ".env의 LLM_*", "역할별 모델"],
    ["llm.payload_extra · pricing", "need_origin: true · 단가표", "게이트웨이 필드, 비용"],
    ["report.language · javax_api_inventory", "en (회사 ko) · true", "보고서 언어, javax jar 인벤토리"],
    ["layout.boot_as_is · multi_module_policy · web_xml_registrations", "in-place · merge · generate", "재구성 동작"]], 0.5, 1.15, 9, [3.6, 2.5, 2.9], { size: 9 }); }

{ const s = content("CLI와 웹 UI", "운영과 검증", "웹 실행은 한 번에 하나(queued). stop은 노드 사이에서만 효과가 있습니다.");
  table(s, [["명령", "용도"],
    ["sbmig run <dir|zip> --config …", "실행(--stop-after, --dry-run, --llm-plugin, --fix-plugin)"],
    ["sbmig analyze / plan / inventory", "변경 없는 분석 / 계획 / pom 없는 트리 설명"],
    ["sbmig llm-probe [--bare]", "요청 하나로 게이트웨이 확인"],
    ["sbmig recipes check --jars", "카탈로그 id를 jar로 검증"],
    ["sbmig advisories check", "advisory 파일 검증"],
    ["sbmig kb learning / promote / reject", "KB 관리"],
    ["sbmig fleet run / status [--attention] / resume", "플릿"],
    ["sbmig clean [--older-than] [--dry-run]", "끝난 실행의 사본 · 업로드 · 기록 정리"],
    ["sbmig serve [--token]", "웹 UI(FastAPI on uvicorn)"]], 0.5, 1.15, 5.2, [2.6, 2.6], { size: 9.5 });
  card(s, 5.9, 1.15, 3.6, 3.9, "웹 REST", ["POST /api/runs (zip) → 202", "GET /api/runs/{id}/events: SSE", "POST /api/runs/{id}/stop", "GET /api/runs/{id}/files/{name}", "GET /api/threads: 스레드 덤프", "토큰 없으면 루프백만, LAN은 --token / SBMIG_WEB_TOKEN"], { bodySize: 11 }); }

{ const s = content("로그, journal, Windows", "운영과 검증", "운영자가 문제를 보고할 때는 sbmig.log, journal.jsonl, 워치독 줄, 스레드 덤프를 봅니다.");
  card(s, 0.5, 1.15, 4.4, 3.9, "어디를 보나", ["지금 단계: sbmig.log 끝, 마지막 phase_start / step_start", "왜 이 스텝: plan.json notes, sbmig plan", "빌드 실패 이유: journal build의 log 경로, sbmig parse-log", "성공 여부: 마지막 report 줄의 success / flags, SBMIG-RESULT 한 줄", "멈춤: 워치독 줄, threads-*.txt, /api/threads"], { bodySize: 11 });
  card(s, 5.1, 1.15, 4.4, 3.9, "Windows", ["git / Maven 출력은 UTF-8로 직접 디코딩, MAVEN_OPTS에 stdout / stderr UTF-8", "changes.patch는 git diff 원본 바이트", "Maven 타임아웃 시 taskkill /PID <pid> /T /F", "git: 비대화형 + 타임아웃, core.longpaths / autocrlf=false / quotepath=false", "덤프: 자동(300초), Ctrl+Break, dump-threads 파일, /api/threads"], { bodySize: 10.5 }); }

{ const s = content("검증", "운영과 검증", "e2e는 JDK 21, Maven, Maven Central이 필요합니다. 로컬 e2e는 사내 좌표 대신 공개 대체물을 씁니다.");
  stat(s, 0.5, 1.1, 2.2, "2,192", "오프라인 테스트 (not integration)", 32);
  stat(s, 2.75, 1.1, 2.2, "32", "e2e 픽스처 (expected.json)", 32);
  stat(s, 5.0, 1.1, 2.2, "31", "결정적 핸들러", 32);
  stat(s, 7.25, 1.1, 2.25, "50", "레시피 카탈로그 id", 32);
  table(s, [["케이스", "픽스처"],
    ["spring_boot (14)", "boot2, boot2-cloud-legacy, boot2-fix-failures, boot2-hystrix-command, boot2-javax-lib(-optional), boot2-method-rename, boot2-qlrm-javax, boot2-rewrite-excluded, boot3, company-boot-single, legacy-role-parents, multi-module-conflict / -merge"],
    ["pure_java (5)", "enforcer-java8-werror, euckr-legacy, java6-legacy, missing-artifact, pure-java"],
    ["spring_legacy (5)", "company-spring5-web, legacy-external-config, legacy-ibatis, spring5-security-batch, spring5-xml"],
    ["servlet_tomcat (6) · msa (2)", "company-servlet-jsp, ear-sibling-poms, servlet-nonstandard-dirs, servlet-tomcat, servlet-war-overlay, servlet-webxml-legacy · msa, msa-shared-lib"]], 0.5, 2.55, 9, [2.0, 7.0], { size: 9 });
  para(s, "scripts/e2e.sh → out/e2e/summary.md · scripts/demo-kb/run.sh(2회차 LLM 0회) · scripts/fleet-demo.sh", 0.5, 4.75, 9, 0.35, { size: 11, italic: true }); }

{ const s = content("요약", "운영과 검증", "설계의 두 축: 결정적 계획과 빌드 게이트. LLM은 사다리의 마지막에만, 결과는 빌드가 판정합니다.");
  card(s, 0.5, 1.15, 2.9, 3.9, "결정적으로", ["규칙 기반 계획과 카탈로그 레시피", "핸들러 31개, 선제 실행", "KB 레시피와 패치 재생"], { bodySize: 11.5 });
  card(s, 3.55, 1.15, 2.9, 3.9, "빌드로 판정", ["모든 시도는 다음 빌드가 채점", "phase마다 build gate", "실행은 항상 clean package로 끝남"], { bodySize: 11.5 });
  card(s, 6.6, 1.15, 2.9, 3.9, "넘겨주기", ["Handover가 보고서 맨 앞", "blocked는 원인과 제안까지", "플릿 학습으로 다음 프로젝트 비용 감소"], { bodySize: 11.5 }); }

(async () => { await pres.writeFile({ fileName: OUT }); if (applyTheme) await applyTheme(OUT, THEME); console.log("written", OUT); })().catch(e => { console.error(e); process.exit(1); });
