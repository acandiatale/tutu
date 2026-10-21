const pptxgen = require("pptxgenjs");
// Theme colours are written by the pptx skill's apply_theme.js (not vendored here): pass its path in APPLY_THEME_JS,
// otherwise the deck is written with Office's stock palette (content identical).
let applyTheme = null;
if (process.env.APPLY_THEME_JS) { try { ({ applyTheme } = require(process.env.APPLY_THEME_JS)); } catch (e) { console.warn("apply_theme.js not loadable:", e.message); } }
const OUT = process.argv[2] || "sbmig-overview.pptx";
const pres = new pptxgen();
pres.layout = "LAYOUT_16x9";               // 10 x 5.625 in
const THEME = { name: "sbmig Forest", headFontFace: "Cambria", bodyFontFace: "Calibri",
  colors: { dk1: "1C2A1E", lt1: "FFFFFF", dk2: "2C5F2D", lt2: "EEF3EA", accent1: "2C5F2D", accent2: "97BC62", accent3: "E07A5F",
            accent4: "3D5A80", accent5: "F2CC8F", accent6: "81B29A", hlink: "2C5F2D", folHlink: "97BC62" } };
const K = THEME.colors;
pres.theme = { headFontFace: THEME.headFontFace, bodyFontFace: THEME.bodyFontFace };
pres.author = "sbmig"; pres.title = "sbmig — JDK 21 · Spring Boot 4.1 마이그레이션 에이전트"; pres.subject = "설계, 동작 원리, 운영";
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
  { placeholder: { options: { name: "title", type: "title", x: 0.5, y: 0.3, w: 9, h: 0.7, fontSize: 26, bold: true, color: C.text2, align: "left", valign: "middle", margin: 0 }, text: "" } },
  { text: { text: "sbmig 핸드북 · 2026-10", options: { ...FOOT } } },
], slideNumber: { x: 9.0, y: 5.25, w: 0.5, h: 0.25, fontSize: 9, color: "8A8F8A", align: "right" } });

// ---------------------------------------------------------------- helpers
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

// ================================================================ 1. 소개
pres.addSection({ title: "소개" });
{ const s = pres.addSlide({ masterName: "TITLE", sectionTitle: "소개" });
  s.addText("sbmig", { placeholder: "title" });
  s.addText("JDK 21 · Spring Boot 4.1 마이그레이션 에이전트 — 설계, 동작 원리, 운영 안내\n2026년 10월 · 사내 Maven 프로젝트 약 500개 대상", { placeholder: "body" });
  s.addNotes("이 발표는 docs/handbook의 한국어 핸드북 9편과 sbmig-reference.xlsx(표 레퍼런스)를 요약합니다. 세부는 각 문서 번호를 참고하십시오."); }

{ const s = content("무엇을, 어디까지 하는가", "소개", "범위는 '패키징 + 빌드 통과'입니다. 런타임 오류는 as-is 담당자의 몫이며, 리포트의 Handover가 그 인수인계 문서입니다.");
  para(s, "사내 Maven 프로젝트를 JDK 21 + Spring Boot 4.1로 옮기고, 회사 표준 레이아웃·사내 부모 pom(4.0.0)으로 재구성한 뒤, mvn clean package가 통과하는 상태로 넘깁니다.", 0.5, 1.15, 9, 0.7, { size: 14 });
  const cases = [["pure_java", "순수 Java 라이브러리/배치"], ["spring_legacy", "Spring Framework 4/5 (XML·war)"], ["spring_boot", "Spring Boot 2/3 (단일·멀티모듈)"], ["servlet_tomcat", "서블릿·JSP 전용 Tomcat 앱"], ["msa", "배포 단위가 여럿인 서비스 묶음"]];
  cases.forEach(([h, b], i) => card(s, 0.5 + i * 1.84, 2.05, 1.7, 1.25, h, b, { headSize: 12.5, bodySize: 11 }));
  card(s, 0.5, 3.55, 4.4, 1.5, "범위 안", ["pom·소스 변환(OpenRewrite 레시피 + 결정적 핸들러 + LLM 잔여 수정)", "회사 레이아웃 재구성, 사내 부모·필수/선택 아티팩트 적용", "최종 mvn clean package + 인수인계 리포트(Handover)"]);
  card(s, 5.1, 3.55, 4.4, 1.5, "범위 밖 (as-is 담당자)", ["런타임 동작 검증, 외부화 설정 값 채우기(__SET_ME__)", "EOL 라이브러리의 코드 수준 교체(어드바이저리로 안내만)", "Nexus에 없는 아티팩트 프록시 등록"], { fill: "FBEAE5" }); }

{ const s = content("이전 에이전트의 실패, sbmig의 설계 응답", "소개", "두 실패 원인이 설계 전체를 결정했습니다: 결정적 계획과 빌드 게이트.");
  card(s, 0.5, 1.2, 4.4, 1.7, "실패 1 — LLM이 빌드 없이 \"끝났다\"고 보고", "수정 LLM이 파일을 고치고 성공을 선언했지만 실제 빌드는 깨져 있었습니다. 검증 없는 자기 보고.", { fill: "FBEAE5", headColor: C.accent3 });
  card(s, 5.1, 1.2, 4.4, 1.7, "sbmig — 빌드 게이트 (불변식 1)", "모든 수정은 재빌드로만 판정합니다(verdict). 단계는 BUILD SUCCESS일 때만 닫히고, 실행 끝에는 반드시 mvn clean package를 한 번 더 돌려 못 푼 에러를 리포트에 적습니다.");
  card(s, 0.5, 3.1, 4.4, 1.9, "실패 2 — 작은 LLM의 레시피 선택", "어떤 OpenRewrite 레시피를 쓸지 LLM이 골랐고, 선택이 약하거나 틀렸습니다.", { fill: "FBEAE5", headColor: C.accent3 });
  card(s, 5.1, 3.1, 4.4, 1.9, "sbmig — 결정적 계획", "코드가 프로젝트를 분석(케이스·라이브러리·JDK)해 규칙으로 레시피 세트와 단계를 정합니다. 레시피 id는 catalog.yml 한 곳에서 관리하고 드리프트는 테스트로 막습니다. LLM은 결정적 핸들러와 지식 베이스가 못 푸는 잔여 에러에만, 마지막 순서로 투입됩니다."); }

{ const s = content("설계 원칙과 불변식 6가지", "소개", "불변식은 CLAUDE.md에 명문화되어 있고 라운드마다 테스트로 지켜집니다.");
  const inv = [["1  빌드 게이트", "BUILD SUCCESS로만 단계가 끝남. 실행은 항상 최종 clean package + 미해결 목록으로 종료"],
    ["2  회사 특이사항은 설정·지식 YAML에", "좌표, 레이아웃, 레시피, 어드바이저리는 config/·knowledge/. 파이썬 코드에 회사 값 없음"],
    ["3  파일 기반 지식 베이스", "kb/는 jsonl·json·yml 파일. DB 금지 — 복사만으로 공유·백업"],
    ["4  안정된 플러그인 심", "LLMRequest/LLMResponse, FixerLike, sbmig.api.migrate(...)는 바뀌지 않음 — 사내 게이트웨이·에이전트 프레임워크가 여기에 꽂힘"],
    ["5  레시피 id 단일 출처", "knowledge/recipes/catalog.yml. sbmig recipes check + 스냅샷 테스트"],
    ["6  산출물은 out/ 아래에만", "테스트·데모가 저장소의 kb/를 더럽히지 않음"]];
  inv.forEach(([h, b], i) => { const col = i % 2, row = Math.floor(i / 2); card(s, 0.5 + col * 4.6, 1.2 + row * 1.3, 4.4, 1.15, h, b, { headSize: 12.5, bodySize: 11 }); }); }

// ================================================================ 2. 동작 원리
pres.addSection({ title: "동작 원리" });
{ const s = pres.addSlide({ masterName: "SECTION", sectionTitle: "동작 원리" }); s.addText("동작 원리", { placeholder: "title" }); s.addText("파이프라인 · 빌드-수정 루프 · 트리아지 결정 사다리 · 지식 축적", { placeholder: "body" }); }

{ const s = content("파이프라인: 7단계, 각 단계는 빌드로 닫힌다", "동작 원리", "상세: 01-pipeline.md. MSA는 jdk21/spring 단계를 서비스마다 반복합니다(@service).");
  flow(s, [{ t: "intake", sub: "zip 해제 · git 스냅샷 · preflight" }, { t: "analyze", sub: "pom·소스 스캔 → profile.json" }, { t: "plan", sub: "케이스별 레시피·스텝 → plan.json" },
    { t: "jdk21", sub: "UpgradeToJava21 + 수정 루프" }, { t: "spring", sub: "Boot 4 / jakarta 레시피 + 루프" }, { t: "restructure", sub: "회사 레이아웃·부모 적용 + 루프" }, { t: "verify", sub: "mvn clean package → report" }], 0.5, 1.25, 9, 1.05, { size: 11.5, subSize: 9 });
  table(s, [["단계", "게이트 빌드 목표", "LLM 투입", "주요 산출물"],
    ["jdk21", "mvn compile (최대 3회 반복)", "잔여 컴파일 에러", "logs/NNN-rewrite-jdk21.rewrite.log, 빌드 로그"],
    ["spring", "mvn test-compile (최대 6회)", "잔여 에러 (lombok, jakarta API …)", "spring.optional_bump(사내 선택 아티팩트 버전) 포함"],
    ["restructure", "mvn package (최대 6회)", "부모 교체 후 잔여 에러", "{name}-boot/-back/-front, __SET_ME__ 플레이스홀더"],
    ["verify", "mvn clean package 1회", "없음 — 결과만 기록", "report.md/json, changes.patch, result.zip"]], 0.5, 2.55, 9, [1.2, 2.3, 2.3, 3.2], { size: 10.5 });
  para(s, "회사 설정은 plan.parent_phase: spring — 사내 부모를 Boot 레시피 직후(spring 루프 전)에 적용해 루프가 처음부터 사내 부모 체인 기준으로 고치게 합니다. 중단(stop)은 그래프 노드 사이에서만 걸리고, verify 전 중단은 '최종 빌드 없이 중단됨'으로 보고합니다.", 0.5, 4.45, 9, 0.7, { size: 11, italic: true }); }

{ const s = content("빌드-수정 루프: 고치고, 다시 빌드하고, 판정한다", "동작 원리", "상세: 04-triage-and-loop.md. 루프의 모든 시도는 ledger.jsonl에 남습니다.");
  flow(s, [{ t: "빌드", sub: "mvn <goal>, 하트비트 60초" }, { t: "로그 파싱", sub: "ErrorKind 11종, javac 연속행" }, { t: "시그니처·클러스터", sub: "L1/L2, 파일 단위 묶음" }, { t: "결정", sub: "핸들러 > KB > blocked > LLM" }, { t: "적용", sub: "git 커밋 (시도별)" }, { t: "재빌드 = 판정", sub: "해결/부분/실패" }], 0.5, 1.25, 9, 1.0, { size: 11.5, subSize: 9 });
  card(s, 0.5, 2.5, 2.9, 2.55, "반복 한도와 예산", ["단계별 max_iterations (3 / 6 / 6)", "클러스터별 fix round, LLM 호출 예산(budget)", "쿨다운 1회 후 'LLM unavailable'", "예산 소진 → escalate, 리포트로"], { bodySize: 11 });
  card(s, 3.55, 2.5, 2.9, 2.55, "stall level (같은 에러가 반복될 때)", ["레벨이 오를수록 더 넓은 문맥·피드백 라운드·상위 모델(LLM_ESCALATION_MODEL)", "그래도 안 되면 escalate — 무한 루프 없음", "blocked 레지스트리: 한 번 blocked면 다른 단계에서도 재시도 안 함"], { bodySize: 11 });
  card(s, 6.6, 2.5, 2.9, 2.55, "운영자가 보는 것", ["[spring it2] cluster compile:… — asking the LLM", "first error: <원문 한 줄>", "LLM answered — N change(s) in <files> — <verdict>", "fix loop quiet for Ns since '<milestone>' (워치독)"], { bodySize: 10.5 }); }

{ const s = content("트리아지 결정 사다리: LLM은 마지막이다", "동작 원리", "sbmig/triage/decide.py의 순서 그대로입니다. 결정 사유(rationale)는 journal의 decision 이벤트에 남습니다.");
  const steps = [["1", "HANDLER", "결정적 핸들러 23종. 에러 문구(트리거) + 프로필 조건. 0 LLM.", K.dk2], ["2", "KB_RECIPE", "지식 베이스가 승격한 OpenRewrite 레시피를 실행", "3F7A41"],
    ["3", "KB_PATCH", "이전 프로젝트에서 성공한 패치를 재생 (LLM 없이)", "5A9460"], ["4", "blocked 판정", "프로젝트 안에서 못 푸는 문제(외부 jar의 javax API, Nexus에 없는 아티팩트) → 즉시 리포트, 0 LLM", K.accent3],
    ["5", "LLM_FIX", "잔여 에러만. 프롬프트 = 에러 클러스터 + 파일 발췌 + 가까운 pom + 지식 노트 + 어드바이저리 힌트. 재빌드로만 판정", K.accent4]];
  steps.forEach(([n, h, b, fill], i) => { const y = 1.2 + i * 0.78; box(s, 0.5, y, 0.6, 0.66, { fill }); para(s, n, 0.5, y, 0.6, 0.66, { size: 20, bold: true, color: C.background1, align: "center", valign: "middle" });
    box(s, 1.2, y, 8.3, 0.66, { fill: K.lt2 }); para(s, h, 1.35, y + 0.08, 1.7, 0.5, { size: 13, bold: true, color: C.text2, valign: "middle" }); para(s, b, 3.0, y + 0.06, 6.4, 0.56, { size: 11, valign: "middle" }); }); }

{ const s = content("결정적 핸들러: 반복되는 문제는 코드가 푼다", "동작 원리", "전체 23종과 트리거는 엑셀 '핸들러' 시트, 설명은 04 문서 5장. 선제(pre-emptive) 핸들러는 첫 빌드 전에 프로필만 보고 미리 돌아 빌드 1회를 아낍니다.");
  stat(s, 0.5, 1.15, 1.6, "23", "결정적 핸들러");
  stat(s, 2.1, 1.15, 1.6, "0", "LLM 호출 (핸들러 경로)");
  para(s, "핸들러는 두 방식으로 움직입니다. 선제: 분석 프로필에서 조건이 보이면 첫 빌드 전에 적용. 반응형: 빌드 에러 문구가 트리거 정규식에 맞으면 적용. 두 경우 모두 다음 빌드가 판정합니다.", 3.9, 1.2, 5.6, 1.2, { size: 12 });
  table(s, [["핸들러", "언제", "무엇을"],
    ["lombok-version-floor", "IllegalAccessError … LombokProcessor", "lombok 버전 하한, 프로세서 경로 고정, 상속된 annotationProcessorPaths override"],
    ["maven-compiler-plugin-floor", "항상(선제) / invalid target release", "컴파일러 플러그인 ≥ 3.13, <release>21</release>"],
    ["jstl-to-jakarta", "javax JSTL 선언 / 버전 누락", "jakarta JSTL(Boot 관리 버전)로 교체"],
    ["resources-binary-filtering", "MalformedInputException (웹폰트 등)", "nonFilteredFileExtensions 추가 — 필터링은 그대로"],
    ["carry-over-managed-version", "부모 교체 후 version missing", "as-is effective pom에서 잡아둔 버전을 명시"],
    ["source-encoding / strip-utf8-bom", "항상(선제)", "MS949/EUC-KR 소스 인코딩 설정, BOM 제거"],
    ["strip-pom-repositories (opt-in)", "build.pom_repositories: remove", "pom의 <repositories> 제거 (Nexus 미러 전제)"]], 0.5, 2.55, 9, [2.4, 2.9, 3.7], { size: 10 }); }

{ const s = content("LLM 수정: 하는 일과 하지 않는 일", "동작 원리", "회사 게이트웨이(provider company): requests.post(LLM_BASE_URL, Bearer, {model, messages, need_origin}). 키는 .env에서만 읽고 어디에도 쓰지 않습니다.");
  card(s, 0.5, 1.2, 4.4, 3.85, "프롬프트에 들어가는 것", ["에러 클러스터(원문 + 시그니처)와 실패한 파일의 발췌", "가장 가까운 pom.xml (nearest_pom)", "키워드로 고른 지식 노트(knowledge/notes: boot3-to-4, jakarta, jdk21 …)", "어드바이저리 힌트(예: hystrix → Resilience4j 전환 지침)", "merge 충돌로 버려진 사본 등 읽기 전용 참고 파일", "<need_grep>로 추가 문맥 요청 가능 — 3000 파일 / 512 KB / 45초 한도", "응답 = 파일별 변경 제안 → 적용 → 재빌드가 판정 → 피드백 라운드"], { bodySize: 11.5 });
  card(s, 5.1, 1.2, 4.4, 3.85, "지키는 선", ["빌드 없이는 성공이 없음 — 판정은 재빌드뿐", "아티팩트 버전을 '지어내지' 않음: 모르는 아티팩트는 blocked(X3)", "Nexus에 없는 것, 외부 jar 내부는 손대지 않음", "content-filter 응답 = 그 요청의 거절로 기록 (장애 아님)", "호출마다 벽시계 하드 데드라인(timeout + 30초), 30초 하트비트", "역할 모델 분리: LLM_MODEL(수정) · LLM_RECIPE_MODEL · LLM_ESCALATION_MODEL", "모든 요청/응답 기록(llm.record_dir), 키는 로그에 절대 없음"], { bodySize: 11.5, fill: "EAF0F6" }); }

{ const s = content("지식 축적: 500개를 순차로 돌릴수록 LLM이 덜 필요해진다", "동작 원리", "kb/는 파일만으로 이루어진 학습 자산입니다. 복사하면 공유·백업이 끝납니다. 상세: 02-workflows.md 6장, 05 문서.");
  flow(s, [{ t: "실행 중 시도 기록", sub: "ledger.jsonl: 시그니처 → 수정 라벨 → 결과" }, { t: "harvest", sub: "성공 사례 → kb/cases, symbols.json" }, { t: "레시피 후보", sub: "kb/recipes/candidates (예: ChangeMethodName)" }, { t: "사람 검토 · 승격", sub: "sbmig kb promote → active" }, { t: "다음 프로젝트", sub: "KB_PATCH 재생 / active 레시피 자동 포함" }], 0.5, 1.25, 9, 1.1, { size: 11.5, subSize: 9 });
  card(s, 0.5, 2.6, 2.9, 2.45, "KB 매칭이 하는 일", ["같은 L1 시그니처 → 성공 패치 재생(LLM 0회)", "L2 일반 시그니처 → 힌트·레시피 후보", "빌드 없는 성공은 절대 기록되지 않음(no KB credit without a build)"], { bodySize: 11 });
  card(s, 3.55, 2.6, 2.9, 2.45, "수정 라벨(fix label)", ["수정이 '어떤 종류'였는지 결정적으로 분류: method-rename, generic-rewrite …", "라벨이 레시피 승격의 단위", "단순 공백 수정은 generic-rewrite로 걸러짐"], { bodySize: 11 });
  card(s, 6.6, 2.6, 2.9, 2.45, "플릿 데모 결과", ["5개 프로젝트 순차 실행에 LLM 호출 총 1회", "첫 프로젝트에서 배운 ChangeMethodName 후보가 뒤 4개에 적용", "ledger가 플릿 요약(summary)의 근거"], { bodySize: 11 }); }

// ================================================================ 3. 회사 레이아웃
pres.addSection({ title: "회사 레이아웃" });
{ const s = pres.addSlide({ masterName: "SECTION", sectionTitle: "회사 레이아웃" }); s.addText("회사 레이아웃과 재구성", { placeholder: "title" }); s.addText("{name}-boot · {name}-back · {name}-front, 사내 부모 3종(4.0.0), Boot as-is는 디렉터리 유지", { placeholder: "body" }); }

{ const s = content("Boot가 아닌 프로젝트: 세 모듈로 분리한다", "회사 레이아웃", "규칙 출처: 사용자 지정(2026-10-21). 상세와 케이스별 before/after: 06-restructure-and-layouts.md.");
  para(s, "as-is (Spring Framework war 예)", 0.5, 1.15, 4.2, 0.3, { size: 12, bold: true, color: C.text2 });
  mono(s, "shop/\n├─ pom.xml            (war, 외부 부모 없음)\n└─ src/main/\n   ├─ java/.../web/HomeController.java\n   ├─ java/.../service/GreetingService.java\n   ├─ resources/applicationContext.xml\n   └─ webapp/\n      ├─ index.html, css/, js/\n      └─ WEB-INF/web.xml, jsp/", 0.5, 1.5, 4.2, 2.2, { size: 9.5, fill: K.lt2 });
  s.addShape(pres.shapes.RIGHT_ARROW, { x: 4.8, y: 2.35, w: 0.4, h: 0.4, fill: { color: K.accent2 }, line: { color: K.accent2 } });
  para(s, "to-be (회사 레이아웃)", 5.3, 1.15, 4.2, 0.3, { size: 12, bold: true, color: C.text2 });
  mono(s, "shop/\n├─ pom.xml      parent=xxx-parent 4.0.0 (pom, 필수 2)\n├─ shop-boot/   parent=xxx-boot-jar  - Boot 설정만\n│  └─ Application.java, application.properties\n├─ shop-back/   parent=xxx-comp-jar  - 모든 소스\n└─ shop-front/  parent=xxx-comp-jar  - 웹 리소스 있을 때\n   └─ resources/static/…, META-INF/…/WEB-INF/jsp/", 5.3, 1.5, 4.2, 2.2, { size: 9.5, fill: K.lt2 });
  bullets(s, ["{name} = 루트 artifactId. 루트는 집계(aggregator) pom이며 필수 의존성 2개는 모든 루트 pom에 들어갑니다(artifacts.mandatory_target: root).",
    "JSP가 있으면 boot 모듈은 WAR(외부 Tomcat), front는 jar — Servlet 3 규칙(META-INF/resources). JSP가 없으면 실행 가능 jar.",
    "사내 선택 아티팩트 8개는 이미 쓰고 있을 때만 4.0.0으로 올립니다. 새 부모가 관리하는 버전은 pom에서 제거합니다(drop_managed_versions)."], 0.5, 3.9, 9, 1.25, 11.5); }

{ const s = content("Boot as-is / MSA: 디렉터리는 그대로, pom만 바꾼다", "회사 레이아웃", "boot_pom 모드. 이전 사내 도구로 이미 마이그레이션된 프로젝트(외부 역할 부모 1.0.0)도 같은 경로로 4.0.0 부모로 옮깁니다.");
  table(s, [["모듈", "판별", "새 부모", "그 밖의 처리"],
    ["루트 pom", "집계 pom", "xxx-parent 4.0.0", "필수 의존성 2개, <relativePath/>, 관리 버전 정리"],
    ["*-boot 모듈", "이름 패턴 + pom이 Boot 앱인지 확인(이중 검사, 경고)", "xxx-boot-jar 4.0.0", "spring-boot-maven-plugin repackage 유지, loaderImplementation=CLASSIC 제거"],
    ["그 외 모듈", "라이브러리·서비스 모듈", "xxx-comp-jar 4.0.0", "repackage 금지"],
    ["단일 모듈 Boot 앱", "모듈 없음", "루트 xxx-parent + {name}-boot로 감쌈(wrap)", "소스는 {name}-boot로 이동"]], 0.5, 1.2, 9, [1.6, 3.0, 1.8, 2.6], { size: 10.5 });
  card(s, 0.5, 3.35, 4.4, 1.7, "MSA", ["서비스마다 jdk21/spring 단계 반복(@service), 부모·필수·선택은 서비스별 동일 규칙", "외부화 설정 인벤토리도 서비스별(PropertyFinding.service)", "디렉터리 구조 불변 — pom-only"], { bodySize: 11 });
  card(s, 5.1, 3.35, 4.4, 1.7, "멀티모듈(비 Boot) merge 정책", ["as-is boot-jar 모듈은 유지, 나머지 모듈 + 루트 src/는 comp 모듈로 접음", "같은 FQCN 충돌은 first-wins, 버려진 사본은 merge-conflicts/에 보관하고 LLM 참고·Handover에 기록", "merge_collision: fail로 엄격 모드"], { bodySize: 11 }); }

{ const s = content("재구성이 함께 처리하는 것", "회사 레이아웃", "각 항목은 결정적이며 결과는 RestructureResult와 리포트 'What we changed'에 남습니다.");
  const items = [["역할 부모 3종", "루트 xxx-parent, boot xxx-boot-jar, 그 외 xxx-comp-jar. 키 기반 매칭 — 실제 좌표는 config/artifacts.yml에만"],
    ["필수 2 · 선택 8", "필수는 모든 루트 pom에 추가, 선택은 사용 중일 때만 4.0.0으로 bump(spring.optional_bump)"],
    ["관리 버전 정리", "as-is effective pom과 새 부모 체인의 effective pom을 두 번 캡처 — 부모가 관리하는 버전은 제거, 잃어버린 버전은 carry-over"],
    ["외부화 설정", "코드가 읽지만 어디에도 정의되지 않은 키 → boot 모듈 application.properties에 __SET_ME__ + Handover 목록 (JNDI → spring.datasource.*)"],
    ["JSP · 웹 리소스", "static → resources/static, JSP → META-INF/resources/WEB-INF/jsp, SpringBootServletInitializer, tomcat/jasper provided, JSTL은 Boot 관리"],
    ["실패 보고", "재구성이 실패하면 성공으로 위장하지 않음: flag restructure-failed, exit 2, verify는 그래도 실행"]];
  items.forEach(([h, b], i) => { const col = i % 3, row = Math.floor(i / 3); card(s, 0.5 + col * 3.05, 1.2 + row * 1.95, 2.9, 1.8, h, b, { headSize: 12.5, bodySize: 10.5 }); }); }

// ================================================================ 4. 아키텍처
pres.addSection({ title: "아키텍처" });
{ const s = pres.addSlide({ masterName: "SECTION", sectionTitle: "아키텍처" }); s.addText("아키텍처", { placeholder: "title" }); s.addText("패키지 지도 · 실행 엔진(LangGraph) · 웹 UI(FastAPI/uvicorn) · 설정 계층 · 플러그인 심", { placeholder: "body" }); }

{ const s = content("패키지 지도: 데이터는 왼쪽에서 오른쪽으로 흐른다", "아키텍처", "상세: 03-architecture.md 2장. 모든 패키지는 sbmig/ 아래에 있고 core의 모델·설정·인터페이스만 공유합니다.");
  const groups = [["core", "models · config · interfaces · workspace(git) · diag", K.dk2], ["analyze · plan", "pom/소스 스캔, 케이스 판정, 라이브러리 규칙, 외부화 설정 인벤토리 → 결정적 플랜, 선제 핸들러 선택, 레시피 카탈로그", "3F7A41"],
    ["rewrite · build", "레시피 합성(compose)과 rewrite-maven-plugin 실행 · Maven 실행, 로그 파서, 시그니처, effective pom, classpath", "5A9460"],
    ["triage · fix · loop · kb", "클러스터·결정·blocked 판정·핸들러 · LLM 수정기(프롬프트, 적용) · 오케스트레이터와 빌드-수정 루프, 예산 · 파일 KB, ledger, harvest, 라벨", K.accent4],
    ["restructure · advisory · report", "레이아웃 재구성, pom 편집기, 템플릿 · 어드바이저리 평가와 힌트 · report.md/json, Handover", "6B8FB5"],
    ["graph · web · fleet · cli · api", "LangGraph StateGraph 엔진 · FastAPI + SSE 웹 UI · 순차 플릿 · typer CLI · sbmig.api.migrate()", "8A8F8A"]];
  groups.forEach(([h, b, fill], i) => { const col = i % 2, row = Math.floor(i / 2); const x = 0.5 + col * 4.6, y = 1.2 + row * 1.3; box(s, x, y, 4.4, 1.15, { fill }); para(s, h, x + 0.15, y + 0.08, 4.1, 0.35, { size: 13, bold: true, color: C.background1 }); para(s, b, x + 0.15, y + 0.45, 4.1, 0.68, { size: 10.5, color: C.background1 }); }); }

{ const s = content("실행 환경: LangGraph + uvicorn (회사 규약)", "아키텍처", "loop.engine: graph(기본) | inline — 두 엔진의 리포트는 테스트로 동일함을 보장합니다. 한 번에 한 실행만 돌고 나머지는 queued.");
  flow(s, [{ t: "intake" }, { t: "analyze" }, { t: "plan" }, { t: "phase ×N", sub: "jdk21 · spring · restructure · verify" }, { t: "finalize", sub: "report · zip · patch" }], 0.5, 1.2, 9, 0.9, { size: 12, subSize: 9 });
  para(s, "LangGraph StateGraph 노드 (sbmig/graph/engine.py) — 중단(stop)은 노드 사이에서만 반영", 0.5, 2.15, 9, 0.3, { size: 10.5, italic: true });
  card(s, 0.5, 2.6, 4.4, 2.45, "웹 UI (sbmig serve / uvicorn sbmig.web.app:app)", ["업로드 → queued → running → finished, 실행당 워커 스레드 1개", "SSE 이벤트: node · journal · log · report (시각순 병합)", "실행별 sbmig.log(스레드 필터), result.zip·report 다운로드", "/api/threads: 모든 스레드 스택 (Windows 진단용), 종료 목록 정리(max_finished_runs)"], { bodySize: 11 });
  card(s, 5.1, 2.6, 4.4, 2.45, "작업공간과 격리", ["out/<run id>/project/ 에 임시 git 저장소: 브랜치 sbmig/<run id>, 스텝·시도마다 커밋, 실패 시 되돌림", "git은 비대화식(프롬프트·훅·서명 끔, 10분 타임아웃), 출력은 UTF-8로 직접 디코딩", "result.zip에는 .git 없음, changes.patch는 원본 바이트", "인코딩 감지: MS949/EUC-KR 소스를 깨뜨리지 않음"], { bodySize: 11 }); }

{ const s = content("설정 계층과 비밀 관리", "아키텍처", "상세: 08-config-reference.md. 엑셀 '설정키' 시트에 186개 키 전체가 있습니다.");
  flow(s, [{ t: ".env → 환경 변수", sub: "LLM_BASE_URL · LLM_API_KEY · LLM_MODEL (이미 있는 쉘 변수가 이김)" }, { t: "config/default.yml", sub: "모든 키의 기본값 (코드와 배포)" }, { t: "--config artifacts.yml", sub: "회사 값: 좌표·레이아웃·parent_phase (여러 파일, 순서대로)" }, { t: "CLI 플래그", sub: "--java-home, --settings, --stop-after …" }, { t: "SBMIG_* 환경 변수", sub: "마지막에 덮어씀(최우선), ${VAR} 확장" }], 0.5, 1.2, 9, 1.15, { size: 11, subSize: 9 });
  card(s, 0.5, 2.6, 4.4, 2.45, "회사에서 처음 채우는 값", ["artifacts.parent / boot_parent / comp_parent = xxx-parent / xxx-boot-jar / xxx-comp-jar 4.0.0", "artifacts.mandatory ×2, artifacts.optional ×8", "plan.parent_phase: spring", "build.java_home (JDK 21), build.settings_file (Nexus catch-all 미러)", ".env: LLM_BASE_URL, LLM_API_KEY, LLM_MODEL (+ LLM_RECIPE_MODEL, LLM_ESCALATION_MODEL)"], { bodySize: 11 });
  card(s, 5.1, 2.6, 4.4, 2.45, "비밀과 Nexus", ["API 키는 환경 변수에서만 읽고 yml·로그·리포트 어디에도 쓰지 않음", "pom에 적힌 옛 저장소 URL은 settings.xml의 mirrorOf external:* 로 전부 Nexus group으로 — 템플릿 config/settings.nexus.example.xml", "미러가 없으면 분석이 follow-up으로 경고, 선택: build.pom_repositories: remove", "Nexus group이 프록시할 것: Central(org.openrewrite.*), Boot 4.1.0, jakarta, 사내 4.0.0"], { bodySize: 11, fill: "EAF0F6" }); }

// ================================================================ 5. 운영
pres.addSection({ title: "운영" });
{ const s = pres.addSlide({ masterName: "SECTION", sectionTitle: "운영" }); s.addText("운영과 진단", { placeholder: "title" }); s.addText("\"멈춘 건가?\" 판단법 · 리포트 읽는 법 · 첫 회사 실행에서 배운 것", { placeholder: "body" }); }

{ const s = content("\"멈춘 건가?\" — 조용해도 정상인 구간과 확인할 세 줄", "운영", "회사 PC는 Windows·망분리라 로그를 손으로 옮겨 적습니다. 그래서 진단은 '세 줄'로 끝나게 설계했습니다. 상세: 09-operations.md 4장.");
  table(s, [["조용한 구간", "정상 신호", "비정상 기준"],
    ["OpenRewrite 실행 (Boot 4 umbrella는 수 분)", "rewrite … still running 60초마다", "openrewrite.timeout_minutes(40) 초과 → 자동 실패 처리"],
    ["LLM 호출", "still in flight 30초마다, 호출 시작/재시도 로그", "timeout + 30초 벽시계 데드라인 → 그 호출 실패, 쿨다운 후 재시도"],
    ["Maven 빌드", "mvn … still running 60초마다", "build.timeout_minutes(30) → Windows taskkill /T /F"],
    ["수정 루프 전체", "마일스톤 진행 로그", "fix loop quiet for Ns since '<milestone>' (60초), 5분이면 threads-*.txt 자동 덤프"]], 0.5, 1.2, 9, [2.7, 3.0, 3.3], { size: 10.5 });
  card(s, 0.5, 3.45, 4.4, 1.6, "개발자에게 보낼 세 줄", ["워치독 줄: fix loop quiet for Ns since '…'", "클러스터의 first error: 줄", "/api/threads 또는 threads-*.txt의 --- thread sbmig-run- 블록"], { bodySize: 11.5 });
  card(s, 5.1, 3.45, 4.4, 1.6, "PowerShell 한 줄", ["Get-Content out\\web\\<id>\\sbmig.log -Wait -Tail 20", "Select-String -Path <log> -Pattern 'first error|quiet for'", "curl.exe http://127.0.0.1:8765/api/threads"], { bodySize: 11 }); }

{ const s = content("리포트 읽는 법: Handover부터", "운영", "report.md와 report.json은 같은 내용입니다. exit code: 0 성공·blocked 없음, 2 blocked/미해결 있음, 3 중단.");
  const secs = [["Result", "최종 clean package 결과, 케이스, as-is 버전, LLM 호출·토큰"], ["Handover (인수인계)", "미해결 에러 · blocked(외부 원인) · 어드바이저리 · 실행 전 채울 속성(__SET_ME__) · merge 충돌 · LLM이 고친 파일"],
    ["Unresolved errors", "최종 빌드에 남은 에러와 마지막 로그 경로"], ["Manual follow-ups", "사람이 해야 할 일 목록(설정 누락, Nexus 프록시, 런타임 확인)"], ["What we changed", "실행된 스텝, OpenRewrite 실행 표, 핸들러·재구성 변경"], ["flags", "tests-skipped, restructure-failed, source-encoding-set:cp949, enforcer-skipped … (exit code를 바꾸지 않는 주의 표시)"]];
  secs.forEach(([h, b], i) => { const y = 1.2 + i * 0.64; box(s, 0.5, y, 2.4, 0.56, { fill: i === 1 ? K.accent3 : K.dk2 }); para(s, h, 0.6, y, 2.2, 0.56, { size: 12, bold: true, color: C.background1, valign: "middle" }); box(s, 3.0, y, 6.5, 0.56, { fill: K.lt2 }); para(s, b, 3.15, y, 6.2, 0.56, { size: 11, valign: "middle" }); }); }

{ const s = content("첫 회사 실행(2026-10-21, Windows)에서 배운 것", "운영", "모두 같은 날 수정되어 main에 반영되었고 테스트가 추가되었습니다. FAQ: 09-operations.md 9장.");
  table(s, [["증상", "원인", "조치"],
    ["게이트웨이 503 (Postman은 성공)", "요청 플래그 철자 need_orgin", "need_origin으로 수정, sbmig llm-probe로 요청 원문 비교"],
    ["'spring it2 … asking the LLM' 뒤 무응답", "nearest_pom이 Windows 절대 경로에서 무한 루프", "경로 정규화 + 반복 상한, 워치독·/api/threads·자동 스레드 덤프 추가"],
    ["lombok IllegalAccessError 반복", "부모가 상속한 옛 프로세서 경로", "lombok-version-floor: 경로 고정, annotationProcessorPaths override"],
    ["JSP 'token recognition error'", "OpenRewrite XML 파서가 JSP를 읽음", "openrewrite.exclusions (jsp, html)"],
    ["javax.servlet:jstl 버전 누락", "옛 부모 체인이 관리하던 버전", "jstl-to-jakarta 핸들러"],
    ["fontawesome.woff2 MalformedInputException", "resources-plugin 3.x가 필터링 리소스의 바이너리를 텍스트로 읽음", "resources-binary-filtering (nonFilteredFileExtensions), 선제 감지"],
    ["UnicodeDecodeError cp949 · changes.patch 실패", "git 출력을 콘솔 코드페이지로 디코딩", "바이너리 git + UTF-8, patch는 원본 바이트"],
    ["pom의 옛 저장소 URL로 접속 실패", "망분리 PC에서 pom 선언 저장소 접근", "settings.xml catch-all 미러 템플릿, 경고 follow-up, pom_repositories: remove"]], 0.5, 1.2, 9, [3.0, 3.0, 3.0], { size: 9.5 }); }

// ================================================================ 6. 현황과 다음 단계
pres.addSection({ title: "현황과 다음 단계" });
{ const s = content("검증 현황", "현황과 다음 단계", "오프라인 테스트는 Python만으로 약 3분, e2e는 JDK 21 + Maven + Maven Central 접근이 필요합니다.");
  stat(s, 0.5, 1.15, 2.2, "1,369", "오프라인 테스트 (통과)");
  stat(s, 2.7, 1.15, 2.2, "24", "e2e 픽스처 프로젝트");
  stat(s, 4.9, 1.15, 2.2, "8 / 8", "회사 레이아웃 e2e PASS (0 LLM)");
  stat(s, 7.1, 1.15, 2.2, "23 / 33 / 14", "핸들러 · 레시피 · 어드바이저리", 26);
  s.addChart(pres.charts.BAR, [{ name: "오프라인 테스트 수", labels: ["R6", "R7a", "R7b", "R8", "R9", "R9b", "R9c", "현재"], values: [917, 941, 991, 1072, 1200, 1233, 1295, 1369] }],
    { x: 0.5, y: 2.65, w: 5.6, h: 2.45, barDir: "col", chartColors: [K.dk2], showTitle: true, title: "라운드별 오프라인 테스트 수", titleFontSize: 11, titleColor: K.dk2, titleFontFace: "+mn-lt",
      showValue: true, dataLabelPosition: "outEnd", dataLabelFontSize: 8.5, dataLabelColor: K.dk1, dataLabelFontFace: "+mn-lt", showLegend: false,
      catAxisLabelColor: K.dk1, catAxisLabelFontSize: 9, catAxisLabelFontFace: "+mn-lt", valAxisLabelColor: "8A8F8A", valAxisLabelFontSize: 8, valAxisLabelFontFace: "+mn-lt",
      valGridLine: { color: "E3E7E1", size: 0.5 }, catGridLine: { style: "none" }, valAxisMinVal: 0, valAxisMaxVal: 1500 });
  card(s, 6.3, 2.65, 3.2, 2.45, "e2e가 확인하는 것", ["실제 Maven으로 끝까지 실행, exit code·blocked·LLM 호출 수 상한", "결과 pom의 부모·패키징·모듈 목록, 외부화 설정 플레이스홀더", "journal 이벤트(restructure mode 등)와 단계 결과", "핸드북 자체도 테스트: 적은 경로·설정 키·명령이 실제로 존재하는지"], { bodySize: 10.5 }); }

{ const s = content("열린 결정과 다음 단계", "현황과 다음 단계", "결정은 회사 몫이며, 각 항목은 CLAUDE.md 'Known follow-ups'에 적혀 있습니다.");
  card(s, 0.5, 1.2, 4.4, 1.85, "회사가 정할 것", ["플릿에서 단위 테스트를 돌릴지 (build.skip_tests: true가 기본 — 빠르지만 런타임 문제를 덜 잡음)", "작업공간에 임시 git 저장소를 두는 것이 허용되는지 (금지면 복사 기반 스냅샷 백엔드)", "xxx-boot-jar 부모가 spring-boot-maven-plugin 버전을 관리하는지 (아니면 parent_manages_spring_boot: false)"], { bodySize: 11 });
  card(s, 5.1, 1.2, 4.4, 1.85, "다음 라운드 후보", ["빌드/리라이트 로그 줄마다 시각 + compile/parse/recipes 분할 요약(큰 프로젝트 시간 분석)", "심볼 단위 교차 파일 클러스터링(같은 라이브러리 메서드 N파일 → LLM 1회)", "ribbon/zuul/netflix-feign 등 guide 어드바이저리의 레시피화", "ADVISOR_CATALOG를 사내 레시피 인벤토리로 확장"], { bodySize: 11 });
  card(s, 0.5, 3.25, 9, 1.8, "플릿 운영 제안", ["1) 대표 프로젝트 4종(순수 Java, Spring 5 war, Boot 2.x 멀티모듈, MSA)으로 실제 Nexus·게이트웨이에서 파일럿 → Handover 품질 확인", "2) kb/를 공유 디렉터리로 두고 플릿을 순차 실행, 매일 ledger 요약과 레시피 후보 검토(sbmig kb learning / promote)", "3) blocked·follow-up 통계로 Nexus 프록시 누락, 사내 라이브러리 jakarta 빌드 등 '프로젝트 밖' 과제를 모아 처리"], { bodySize: 11.5 }); }

{ const s = content("부록: 문서 지도", "현황과 다음 단계", "제출물: docs/handbook/ 의 마크다운 9편 + 이 PPT + sbmig-reference.xlsx.");
  table(s, [["문서", "내용", "주 독자"],
    ["README.md (00)", "개요, 역할별 읽는 순서, 용어집, 제출물 안내", "모두"], ["01-pipeline.md", "단계별 입력·출력·파일·분기·저널 이벤트, 실제 실행 타임라인", "운영자·개발자"],
    ["02-workflows.md", "CLI/웹 실행, LLM 점검, 플릿, 지식 축적, 레시피 점검, 어드바이저리, 외부화 설정, e2e, 결과 받기", "운영자·담당자"],
    ["03-architecture.md", "원칙·불변식, 패키지 지도, 데이터 모델, 설정 계층, 엔진, 웹, 작업공간, 진단, 플러그인, 타임아웃", "개발자"],
    ["04-triage-and-loop.md", "로그 파싱→결정 사다리→루프 제어, 핸들러 카탈로그, 로그 한 줄 해석표, 실제 시나리오 3개", "모두"],
    ["05-directories-and-storage.md", "config/ knowledge/ kb/ fixtures/ out/<run id>/ tests/ scripts/ 의 저장 형식과 활용", "운영자·개발자"],
    ["06-restructure-and-layouts.md", "회사 레이아웃 규칙, 모드별 before/after, 템플릿, 부모·버전, merge, JSP/WAR", "담당자·개발자"],
    ["07-api-reference.md · 08-config-reference.md", "CLI·REST·Python API·플러그인 계약·스키마 / 설정 키 전체", "개발자 / 운영자"],
    ["09-operations.md", "사전 준비, Nexus 미러, Windows, 리포트 읽기, 멈춤 진단, 에러 카탈로그(한국어), 플릿, KB 위생, FAQ", "운영자"],
    ["sbmig-reference.xlsx", "설정키 · 핸들러 · 에러카탈로그 · CLI · REST_API · 픽스처 · 저널이벤트 · 리포트플래그 · 레시피카탈로그 · 어드바이저리 · 지식노트 · 열거형 · 디렉터리", "모두"]], 0.5, 1.2, 9, [2.6, 5.2, 1.2], { size: 9.5 }); }

(async () => { await pres.writeFile({ fileName: OUT }); if (applyTheme) await applyTheme(OUT, THEME); console.log("written", OUT); })().catch(e => { console.error(e); process.exit(1); });
