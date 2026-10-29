// migration agent — 담당자용 흐름 안내 덱 (2026-10-29): 전체 흐름도 한 장 + 단계별 설명 + 수정 루프.
// Run from scripts/handbook:  NODE_PATH=$PWD/node_modules APPLY_THEME_JS=<pptx skill>/scripts/apply_theme.js node build_migration_agent_deck.js ../../docs/migration-agent/migration-agent.pptx
// Diagram PNGs: docs/migration-agent/diagrams/flow-*.mmd rendered with mermaid-cli (npx -y -p @mermaid-js/mermaid-cli mmdc -i x.mmd -o x.png -b white -s 3 -c flow-config.json).
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
pres.author = "sbmig"; pres.title = "마이그레이션 에이전트 흐름 안내"; pres.subject = "프로젝트 하나가 처리되는 흐름";
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
  { text: { text: "마이그레이션 에이전트 · 흐름 안내", options: { ...FOOT } } },
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
// ---------------------------------------------------------------- 그림 슬라이드: 그림을 크게, 설명은 아래 두세 줄
function picture(title, name, lines, notes, o = {}) {
  const s = content(title, "흐름", notes);
  const lh = o.linesH || 1.0;
  const area = 5.15 - 1.05 - lh - 0.1;
  fitImage(s, name, 0.5, 1.05, 9, area);
  bullets(s, lines, 0.5, 5.15 - lh, 9, lh, o.size || 12);
  return s;
}
function legend(s, y) {
  const items = [["EEF3EA", "자동으로 처리"], ["F2CC8F", "Maven 빌드"], ["3D5A80", "LLM 이 개입"], ["E07A5F", "반복 · 사람에게 인계"], ["2C5F2D", "끝"]];
  let x = 0.5;
  items.forEach(([c, t]) => {
    s.addShape(pres.shapes.RECTANGLE, { x, y: y + 0.05, w: 0.22, h: 0.18, fill: { color: c }, line: { color: c } });
    s.addText(t, { x: x + 0.27, y, w: 1.6, h: 0.28, fontSize: 10.5, color: C.text1, margin: 0, isTextBox: true, valign: "middle" });
    x += 1.85;
  });
}

addSec("흐름");
{ const s = pres.addSlide({ masterName: "TITLE", sectionTitle: "흐름" });
  s.addText("마이그레이션 에이전트, 이렇게 돌아갑니다", { placeholder: "title" });
  s.addText("프로젝트 하나를 넣으면 무슨 일이 일어나는지, 단계별로", { placeholder: "body" });
  s.addNotes("담당자 설명용 덱입니다. 설정 키나 파일 이름 같은 세부 사항은 뺐고, 흐름과 원리만 담았습니다."); }

{ const s = content("전체 흐름", "흐름", "LLM 이 개입하는 곳은 ⑦ 구조 검토와 ⑨ 수정 루프 두 곳뿐입니다. 나머지는 정해진 규칙대로 돕니다. 고쳐졌는지는 언제나 다음 빌드 결과로 판단합니다.");
  fitImage(s, "flow-overview", 0.5, 1.02, 9, 3.55);
  legend(s, 4.62);
  para(s, "LLM 이 개입하는 곳은 두 곳(⑦, ⑨)뿐이고, 고쳐졌는지는 언제나 다음 빌드 결과로 판단합니다.", 0.5, 4.92, 9, 0.3, { size: 11, italic: true }); }

{ const s = content("접수와 분석", "흐름", "원본은 건드리지 않습니다. 작업용 사본에서 모든 단계를 git 으로 기록하므로 언제든 되돌릴 수 있습니다.");
  card(s, 0.5, 1.15, 4.4, 3.9, "① 접수", [
    "프로젝트 폴더나 zip 을 받으면 먼저 Maven 프로젝트인지 봅니다. Gradle 이거나 pom.xml 없이 소스만 있는 폴더는 받지 않고, 이유를 적어 돌려줍니다.",
    "원본은 그대로 두고 작업용 사본을 만듭니다. 이후 모든 수정은 git 으로 기록해서 언제든 되돌릴 수 있습니다.",
    "Maven, JDK 21, 사내 parent 가 준비돼 있는지 미리 점검하고, 없으면 여기서 멈춥니다."], { bodySize: 11.5 });
  card(s, 5.1, 1.15, 4.4, 3.9, "② 분석", [
    "어떤 종류의 프로젝트인지 가립니다: 순수 Java, Spring, Spring Boot, 서블릿, MSA.",
    "어떤 라이브러리를 어떤 버전으로 쓰는지, web.xml 이 있는지, JSP 가 javax 를 쓰는지 읽어 둡니다.",
    "지원이 끝난 라이브러리가 있으면 메모해 두었다가 보고서에 안내합니다. 빌드를 막지는 않습니다."], { bodySize: 11.5, fill: "EEF3EA" }); }

{ const s = content("계획과 자동 변환", "흐름", "변환 규칙을 최소한만 쓰는 이유: 무엇을 바꿀지 모르는 규칙은 돌리지 않는다는 원칙입니다. 나머지는 오류가 나면 그때 고칩니다.");
  card(s, 0.5, 1.15, 4.4, 3.9, "③ 계획", [
    "프로젝트 종류에 따라 돌릴 단계가 정해집니다. 예를 들어 Spring Boot 프로젝트는 Boot 4 규칙을 쓰고, 서블릿 프로젝트는 구조 정리에서 WAR 로 만듭니다.",
    "여기에는 사람이나 LLM 의 판단이 들어가지 않습니다. 같은 프로젝트를 다시 넣으면 같은 계획이 나옵니다.",
    "계획은 파일로 남아서, 왜 이 단계가 돌았는지 나중에 확인할 수 있습니다."], { bodySize: 11.5 });
  card(s, 5.1, 1.15, 4.4, 3.9, "④ 자동 변환", [
    "OpenRewrite 라는 도구로 정해진 규칙만 적용합니다: JDK 21 문법, javax → jakarta 패키지 이름, Spring Boot 4 로 가는 변경.",
    "그 밖의 라이브러리 규칙은 일부러 넣지 않았습니다. 그런 것은 오류로 드러나면 수정 루프에서 고칩니다.",
    "변환은 한 번만 돌리고 중간에 빌드하지 않습니다. 결과는 git 에 기록됩니다."], { bodySize: 11.5, fill: "EEF3EA" }); }

{ const s = content("오류는 한 번에 다 보이지 않습니다", "흐름", "그래서 수정 루프가 필요합니다. 고칠 때마다 다시 빌드해서 새로 드러나는 오류를 이어서 처리합니다.");
  para(s, "⑤ 1차 컴파일: 변환이 끝나면 한 번 컴파일해서 지금 보이는 오류를 전부 목록에 적습니다. 이때는 아직 고치지 않습니다. 이 목록이 이후 모든 수정의 출발점입니다.", 0.5, 1.15, 9, 0.7, { size: 12.5 });
  card(s, 0.5, 2.0, 4.4, 3.05, "왜 한 번에 다 안 보이나", [
    "Maven 은 모듈을 하나씩 순서대로 빌드하다가 실패하면 거기서 멈춥니다. 뒤 모듈의 오류는 앞 모듈이 통과해야 보입니다.",
    "자바 컴파일러도 단계별로 봅니다. 문법 오류가 있으면 타입 오류는 알려 주지 않고, 한 번에 100개까지만 보고합니다."], { bodySize: 11.5 });
  card(s, 5.1, 2.0, 4.4, 3.05, "에이전트는 이렇게 다룹니다", [
    "'오류 하나 고치면 다른 오류가 나오는' 일을 정상으로 봅니다. 고칠 때마다 다시 빌드해서 새로 드러난 오류를 이어서 처리합니다.",
    "오류가 안 보인다고 고쳐진 것으로 치지 않습니다. 빌드가 그 오류가 나던 단계까지 갔을 때만 해결로 셉니다."], { bodySize: 11.5, fill: "EEF3EA" }); }

{ const s = content("구조 정리와 구조 검토", "흐름", "이미 Spring Boot 인 프로젝트는 폴더를 그대로 두고 pom.xml 만 바꿉니다. 구조 검토의 LLM 호출은 최대 3번입니다.");
  card(s, 0.5, 1.15, 4.4, 3.9, "⑥ 구조 정리", [
    "회사 표준에 맞춰 모듈을 나눕니다: 실행 설정만 있는 boot 모듈, 소스가 들어가는 back 모듈, 화면 파일이 있을 때만 front 모듈.",
    "pom.xml 의 parent 를 사내 parent 로 바꾸고, 필수 라이브러리를 넣고, 이미 쓰고 있던 사내 라이브러리만 새 버전으로 올립니다.",
    "JSP 와 정적 파일(html, js, css, 영상)을 제자리로 옮기고, JSP 안의 javax 도 jakarta 로 바꿉니다."], { bodySize: 11.5 });
  card(s, 5.1, 1.15, 4.4, 3.9, "⑦ 구조 검토", [
    "정리된 결과를 LLM 이 회사 규칙으로 한 번 더 봅니다: pom.xml, 모듈 구성, 소스 인코딩, 빌드 중 깨질 수 있는 바이너리 파일.",
    "꼭 고쳐야 하는 것만 바로 고치고 git 에 기록합니다. 나머지는 보고서에 적어 담당자가 판단하게 합니다.",
    "호출은 많아야 3번: 검토 1번, 답이 깨졌을 때 1번, 고친 것이 적용되지 않았을 때 1번."], { bodySize: 11.5, fill: "EEF3EA" }); }

picture("검증 빌드와 수정 루프", "flow-loop", [
  "⑧ mvn clean package 가 성공하면 바로 마무리합니다. 실패하면 ⑨ 수정 루프에 들어가 빌드가 될 때까지 돕니다.",
  "한 바퀴는 고치기 → 다시 빌드 → 채점입니다. 고친 뒤 빌드가 더 나빠지면 그 수정은 되돌립니다. 최대 50바퀴.",
  "두 수정이 같은 파일을 동시에 건드리지 않도록, 방금 손댄 파일의 오류는 다음 빌드 뒤에 봅니다."],
  "라운드 수와 LLM 호출 수는 설정으로 조절합니다. 한 바퀴에 다루는 케이스는 최대 8개입니다.", { linesH: 1.25, size: 11.5 });

picture("오류 하나가 고쳐지는 길", "flow-ladder", [
  "먼저 지식베이스를 봅니다. 다른 프로젝트에서 이미 고친 적 있는 오류면 그때의 스크립트를 그대로 써서 LLM 을 부르지 않습니다.",
  "없거나 안 통하면 LLM 에게 오류와 관련 파일, pom.xml, 라이브러리 버전 정보를 주고 수정을 받습니다. 한 케이스에 최대 3번.",
  "3번 해도 안 되면 그 케이스는 사람 몫입니다. 보고서 맨 앞에 무엇을 시도했는지까지 적어 넘깁니다."],
  "LLM 이 받는 것: 오류 메시지, 오류가 난 파일의 해당 부분, pom.xml 발췌, 저장소에서 조회한 라이브러리 버전 사실, 이전 시도와 실패 이유. 메이저 버전을 올리는 변경은 보고서에 '운영자 확인 필요' 로 따로 적습니다.", { linesH: 1.25, size: 11.5 });

{ const s = content("고쳐졌는지는 빌드가 판정합니다", "흐름", "LLM 이 '고쳤다' 고 말해도 믿지 않습니다. 다음 빌드 결과만 봅니다.");
  table(s, [["다음 빌드 결과", "뜻", "그 다음"],
    ["좋아짐", "노리던 오류가 전부 사라졌고, 빌드가 그 단계까지 갔습니다", "수정을 유지하고 지식베이스에 성공으로 기록"],
    ["일부 좋아짐", "일부만 사라졌고, 손댄 파일에 새 오류는 없습니다", "수정을 유지하고 같은 케이스를 이어서 시도"],
    ["변화 없음", "오류가 그대로입니다", "수정을 되돌리고 다음 방법으로"],
    ["나빠짐", "손댄 파일에 새 오류가 생겼거나 오류가 크게 늘었습니다", "수정을 되돌리고 다시 빌드"],
    ["알 수 없음", "빌드가 시간 초과 등으로 끝까지 가지 못했습니다", "수정을 되돌리고 다음 빌드에서 다시 판정"]], 0.5, 1.15, 9, [1.6, 4.2, 3.2], { size: 11 });
  para(s, "'오류가 안 보인다' 와 '고쳐졌다' 는 다릅니다. 앞 단계에서 빌드가 멈추면 뒤 단계의 오류는 보이지 않을 뿐이므로, 빌드가 그 단계까지 갔을 때만 해결로 셉니다. 시도 없이 사라진 오류는 같은 바퀴에서 그 파일을 고친 수정 덕분으로 기록합니다.", 0.5, 4.2, 9, 0.9, { size: 11, italic: true }); }

picture("한 번 고친 방법은 다음 프로젝트에서 다시 씁니다", "flow-kb", [
  "같은 모양의 오류는 어느 프로젝트에서 나와도 같은 케이스로 모입니다. 케이스마다 수정 스크립트와 성공·실패 횟수, 실패한 이유, 고치기 전·후 코드가 쌓입니다.",
  "프로젝트를 순서대로 돌리면 뒤로 갈수록 LLM 을 덜 부릅니다. 끝나면 보고서 맨 앞에 사람이 확인하거나 이어서 할 일이 적혀 있습니다.",
  "사람이 개입할 수 있습니다: 라이브러리 버전 표를 적어 주기, 잘못 배운 케이스 지우기, 고치지 말라고 표시하기."],
  "지식베이스는 파일 폴더입니다. 데이터베이스가 없으므로 복사해서 다른 PC 로 옮길 수 있습니다.", { linesH: 1.35, size: 11.5 });

(async () => { await pres.writeFile({ fileName: OUT }); if (applyTheme) await applyTheme(OUT, THEME); console.log("written", OUT); })().catch(e => { console.error(e); process.exit(1); });
