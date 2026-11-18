// migration agent — 사용 안내 덱: 프로젝트를 올리는 팀(사용자)과 담당자에게 흐름, 받는 결과, 읽는 법을 보여 준다.
// Run from scripts/handbook:  NODE_PATH=$PWD/node_modules APPLY_THEME_JS=<pptx skill>/scripts/apply_theme.js node build_migration_agent_deck.js ../../docs/migration-agent/migration-agent.pptx
// 흐름도는 전부 파워포인트 도형(둥근 네모 + 선)으로 그린다 — 파워포인트에서 글자·칸을 바로 고칠 수 있다. 그림 파일을 쓰지 않는다.
// 쪽 목록과 확인 명령은 README.md 에 있다.
const pptxgen = require("pptxgenjs");
let applyTheme = null;
if (process.env.APPLY_THEME_JS) { try { ({ applyTheme } = require(process.env.APPLY_THEME_JS)); } catch (e) { console.warn("apply_theme.js not loadable:", e.message); } }
const OUT = process.argv[2] || "migration-agent.pptx";
const pres = new pptxgen();
pres.layout = "LAYOUT_16x9";               // 10 x 5.625 in
const THEME = { name: "sbmig Forest", headFontFace: "Cambria", bodyFontFace: "Calibri",
  colors: { dk1: "1C2A1E", lt1: "FFFFFF", dk2: "2C5F2D", lt2: "EEF3EA", accent1: "2C5F2D", accent2: "97BC62", accent3: "E07A5F",
            accent4: "3D5A80", accent5: "F2CC8F", accent6: "81B29A", hlink: "2C5F2D", folHlink: "97BC62" } };
const K = THEME.colors;
pres.theme = { headFontFace: THEME.headFontFace, bodyFontFace: THEME.bodyFontFace };
pres.author = "sbmig"; pres.title = "마이그레이션 에이전트 사용 안내"; pres.subject = "프로젝트를 올리면 일어나는 일과 받는 결과";
const C = pres.SchemeColor;
const FOOT = { x: 0.5, y: 5.25, w: 5, h: 0.25, fontSize: 9, color: "8A8F8A", margin: 0 };

pres.defineSlideMaster({ title: "TITLE", background: { color: K.dk2 }, objects: [
  { placeholder: { options: { name: "title", type: "title", x: 0.6, y: 1.5, w: 8.8, h: 1.5, fontSize: 38, bold: true, color: C.background1, align: "left", valign: "bottom", margin: 0 }, text: "" } },
  { placeholder: { options: { name: "body", type: "body", x: 0.6, y: 3.15, w: 8.8, h: 1.3, fontSize: 17, color: C.background2, align: "left", valign: "top", margin: 0 }, text: "" } },
]});
pres.defineSlideMaster({ title: "CONTENT", background: { color: K.lt1 }, objects: [
  { placeholder: { options: { name: "title", type: "title", x: 0.5, y: 0.3, w: 9, h: 0.7, fontSize: 24, bold: true, color: C.text2, align: "left", valign: "middle", margin: 0 }, text: "" } },
  { text: { text: "마이그레이션 에이전트 · 사용 안내", options: { ...FOOT } } },
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
  s.addText(items.map((t, i) => ({ text: t, options: { bullet: true, breakLine: i < items.length - 1, paraSpaceAfter: 4 } })),
    { x, y, w, h, fontSize: size, color: C.text1, margin: 0, isTextBox: true, valign: "top" });
}
function para(s, text, x, y, w, h, o = {}) { s.addText(text, { x, y, w, h, fontSize: o.size || 12, color: o.color || C.text1, bold: !!o.bold, italic: !!o.italic, align: o.align || "left", valign: o.valign || "top", margin: 0, isTextBox: true, fontFace: o.font }); }
function table(s, rows, x, y, w, colW, o = {}) {
  const size = o.size || 10.5;
  const head = rows[0].map(t => ({ text: t, options: { bold: true, color: "FFFFFF", fill: { color: K.dk2 }, fontSize: size, valign: "middle" } }));
  const body = rows.slice(1).map((r, ri) => r.map(t => ({ text: t, options: { fontSize: size, color: K.dk1, fill: { color: ri % 2 ? "FFFFFF" : K.lt2 }, valign: "top" } })));
  s.addTable([head, ...body], { x, y, w, colW, border: { type: "solid", color: "D5DBD3", pt: 0.5 }, margin: 0.05, fontFace: THEME.bodyFontFace, autoPage: false });
}

// ---------------------------------------------------------------- 도형으로 그리는 흐름도 (파워포인트에서 편집 가능)
// 칸의 색: 자동 처리 / Maven 빌드 / LLM / 반복·사람 몫 / 끝 / 질문·받는 것 / 취약점 검사와 조치
const KIND = { auto: { fill: "EEF3EA", line: "2C5F2D", text: "1C2A1E" }, build: { fill: "F2CC8F", line: "C9A24C", text: "1C2A1E" },
               llm: { fill: "3D5A80", line: "3D5A80", text: "FFFFFF" }, loop: { fill: "E07A5F", line: "E07A5F", text: "FFFFFF" },
               done: { fill: "2C5F2D", line: "2C5F2D", text: "FFFFFF" }, ask: { fill: "FFFFFF", line: "8A8F8A", text: "1C2A1E" },
               scan: { fill: "E6DFF2", line: "8E7CC3", text: "1C2A1E" } };
const ARROW = "4A4F4A";
// 네모칸 하나 = 도형 + 글(굵은 제목, 작은 설명). 가운데 좌표를 돌려줘서 화살표를 잇는다.
function node(s, x, y, w, h, title, desc, kind = "auto", o = {}) {
  const k = KIND[kind];
  s.addText([{ text: title, options: { bold: true, fontSize: o.titleSize || 11, color: k.text, breakLine: true } },
             { text: desc, options: { fontSize: o.descSize || 8.5, color: k.text } }],
    { shape: pres.shapes.ROUNDED_RECTANGLE, rectRadius: 0.05, x, y, w, h, fill: { color: k.fill }, line: { color: k.line, width: 1 },
      align: o.align || "center", valign: "middle", margin: 0.05, paraSpaceAfter: 2, fontFace: THEME.bodyFontFace });
  return { x, y, w, h, cx: x + w / 2, cy: y + h / 2, l: x, r: x + w, t: y, b: y + h };
}
// 선분: (x1,y1) → (x2,y2). 끝에 화살촉을 달지 선택. 가로·세로·대각선 모두 됨.
function seg(s, x1, y1, x2, y2, o = {}) {
  const x = Math.min(x1, x2), y = Math.min(y1, y2), w = Math.abs(x2 - x1), h = Math.abs(y2 - y1);
  const opts = { x, y, w, h, line: { color: o.color || ARROW, width: o.width || 1.25, dashType: o.dash ? "dash" : "solid" } };
  if (o.head) opts.line.endArrowType = "triangle";
  if (o.tail) opts.line.beginArrowType = "triangle";
  // pptxgenjs 의 선은 왼쪽 위 → 오른쪽 아래. 방향이 다르면 뒤집는다.
  const goesRight = x2 >= x1, goesDown = y2 >= y1;
  if (w > 0 && h > 0) { if (!goesRight && goesDown) opts.flipH = true; if (goesRight && !goesDown) opts.flipV = true; if (!goesRight && !goesDown) { opts.flipH = true; opts.flipV = true; } }
  else if (w > 0 && !goesRight) opts.flipH = true;
  else if (h > 0 && !goesDown) opts.flipV = true;
  s.addShape(pres.shapes.LINE, opts);
}
// 꺾인 화살표: 점 목록을 차례로 잇고 마지막 선분에 화살촉.
function route(s, pts, o = {}) {
  for (let i = 0; i < pts.length - 1; i++) seg(s, pts[i][0], pts[i][1], pts[i + 1][0], pts[i + 1][1], { ...o, head: i === pts.length - 2 && o.head !== false });
}
function label(s, x, y, text, o = {}) {
  const w = o.w || 1.0, h = 0.22;
  s.addText(text, { x: x - w / 2, y: y - h / 2, w, h, fontSize: o.size || 8.5, color: "4A4F4A", align: "center", valign: "middle", margin: 0, isTextBox: true, fill: { color: "FFFFFF" }, fontFace: THEME.bodyFontFace });
}

// ---------------------------------------------------------------- 단계별 쪽: 단계 하나 = 한 쪽, 그림은 steps/<key>.js 가 도형으로 그린다
// steps/<key>.js 는 { title, notes, lines, draw(s, H) } 를 내보낸다. draw 는 AREA 안에만 그린다. 위의 띠와 아래 글은 여기서 놓는다.
// 순서는 코드가 실행하는 순서다: 최종 빌드 → (빌드 성공이면) 취약점 조치 → 마무리(보고서, 지식베이스, result.zip).
const STEPS = [["pod", "파드 준비"], ["intake", "접수"], ["analyze", "분석"], ["plan", "계획"], ["transform", "자동 변환"], ["restructure", "구조 정리"],
               ["review", "구조 검토"], ["verify", "빌드와 수정"], ["remediation", "취약점 조치"], ["finalize", "마무리"]];
const AREA = { x: 0.5, y: 1.4, w: 9, h: 2.75, l: 0.5, r: 9.5, t: 1.4, b: 4.15 };
const H = { node, seg, route, label, para, AREA, KIND };
// 지금 어느 단계인지 보여 주는 띠: 열 칸 가운데 하나만 진하게.
function strip(s, cur) {
  const gap = 0.05, w = (9 - gap * (STEPS.length - 1)) / STEPS.length;
  STEPS.forEach(([key, name], i) => {
    const on = key === cur;
    s.addText(name, { shape: pres.shapes.ROUNDED_RECTANGLE, rectRadius: 0.04, x: 0.5 + i * (w + gap), y: 1.03, w, h: 0.22, fontSize: 8.5, bold: on,
      color: on ? "FFFFFF" : "6B726B", fill: { color: on ? K.dk2 : K.lt2 }, line: { color: on ? K.dk2 : K.lt2, width: 0.5 }, align: "center", valign: "middle", margin: 0, fontFace: THEME.bodyFontFace });
  });
}
function stepSlide(key, section) {
  const m = require("./steps/" + key + ".js");
  const s = content(m.title, section, m.notes);
  strip(s, key);
  m.draw(s, H);
  bullets(s, m.lines, 0.5, 4.3, 9, 0.88, 10.5);
  return s;
}

// ---------------------------------------------------------------- 1. 전체 흐름 — 열두 단계, 세 줄(4 · 4 · 4). 셋째 줄은 오른쪽에서 왼쪽으로 읽는다
function drawOverview(s) {
  const w = 2.0, h = 0.74, gx = 0.333, ys = [1.08, 2.12, 3.16];
  const col = i => 0.5 + i * (w + gx);
  const n = {};
  n[1] = node(s, col(0), ys[0], w, h, "① 파드 준비", "프로젝트를 올리면 그 프로젝트만의 빌드 파드가 뜹니다", "auto");
  n[2] = node(s, col(1), ys[0], w, h, "② 접수", "Maven 프로젝트인지 확인하고 작업용 사본을 만듭니다", "auto");
  n[3] = node(s, col(2), ys[0], w, h, "③ 분석", "프로젝트 종류와 쓰는 라이브러리를 읽어 둡니다", "auto");
  n[4] = node(s, col(3), ys[0], w, h, "④ 계획", "종류에 맞춰 진행할 단계를 정합니다", "auto");
  n[5] = node(s, col(0), ys[1], w, h, "⑤ 자동 변환", "JDK 21, jakarta, Spring Boot 4 규칙을 한 번에 적용합니다", "auto");
  n[6] = node(s, col(1), ys[1], w, h, "⑥ 구조 정리", "회사 표준 구조로 모듈과 pom.xml을 다시 짭니다", "auto");
  n[7] = node(s, col(2), ys[1], w, h, "⑦ 구조 검토", "LLM이 회사 규칙에 맞는지 보고 꼭 필요한 것만 고칩니다", "llm");
  n[8] = node(s, col(3), ys[1], w, h, "⑧ 검증 빌드", "처음으로 전체를 빌드해 봅니다", "build");
  n[9] = node(s, col(3), ys[2], w, h, "⑨ 수정 루프", "LLM의 도움으로 오류를 고치고 다시 빌드합니다", "loop");
  n[10] = node(s, col(2), ys[2], w, h, "⑩ 최종 빌드", "한 번 더 전체를 빌드해 결과를 확정합니다", "build");
  n[11] = node(s, col(1), ys[2], w, h, "⑪ 취약점 조치", "IVAS로 검사하고 취약한 라이브러리 버전을 올립니다", "scan");
  n[12] = node(s, col(0), ys[2], w, h, "⑫ 마무리", "보고서와 결과 파일을 만들고 고친 방법을 저장합니다", "done");
  const right = (p, q) => seg(s, p.r, p.cy, q.l, q.cy, { head: true });
  const left = (p, q) => seg(s, p.l, p.cy, q.r, q.cy, { head: true });
  right(n[1], n[2]); right(n[2], n[3]); right(n[3], n[4]); right(n[5], n[6]); right(n[6], n[7]); right(n[7], n[8]);
  // 줄 바꿈 ④ → ⑤ (아래로 내려와 왼쪽 끝으로)
  const gap1 = (n[4].b + n[5].t) / 2, gap2 = (n[8].b + n[9].t) / 2;
  route(s, [[n[4].cx, n[4].b], [n[4].cx, gap1], [n[5].cx, gap1], [n[5].cx, n[5].t]]);
  // ⑧ 실패 → ⑨ (바로 아래), ⑧ 성공 → ⑪ (⑨ ⑩ 을 건너뜀)
  seg(s, n[8].cx + 0.3, n[8].b, n[9].cx + 0.3, n[9].t, { head: true });
  label(s, n[8].cx + 0.62, gap2, "실패", { w: 0.4 });
  const sx = n[8].l + 0.35;
  route(s, [[sx, n[8].b], [sx, gap2], [n[11].cx, gap2], [n[11].cx, n[11].t]]);
  label(s, (sx + n[11].cx) / 2, gap2, "성공", { w: 0.4 });
  left(n[9], n[10]);
  left(n[10], n[11]); label(s, (n[10].l + n[11].r) / 2, n[10].cy + 0.2, "성공", { w: 0.32 });
  left(n[11], n[12]);
  // ⑩ 실패 → ⑫ (⑪ 아래로 돌아서)
  const below = n[10].b + 0.2;
  route(s, [[n[10].cx, n[10].b], [n[10].cx, below], [n[12].cx, below], [n[12].cx, n[12].b]]);
  label(s, (n[10].cx + n[12].cx) / 2, below, "실패", { w: 0.4 });
}
function legend(s, y) {
  const items = [["EEF3EA", "자동으로 처리"], ["F2CC8F", "Maven 빌드"], ["3D5A80", "LLM이 돕는 곳"], ["E07A5F", "반복해서 고침"], ["E6DFF2", "취약점 검사·조치"], ["2C5F2D", "끝"]];
  let x = 0.5;
  items.forEach(([c, t]) => {
    s.addShape(pres.shapes.RECTANGLE, { x, y: y + 0.05, w: 0.2, h: 0.17, fill: { color: c }, line: { color: c === "E6DFF2" ? "8E7CC3" : c } });
    s.addText(t, { x: x + 0.25, y, w: 1.25, h: 0.27, fontSize: 10, color: C.text1, margin: 0, isTextBox: true, valign: "middle" });
    x += 1.5;
  });
}

// ---------------------------------------------------------------- 2. 프로젝트에 따라 길이 달라집니다
function drawRoads(s) {
  const X = [0.5, 2.55, 5.05, 7.4], W = [1.75, 2.2, 2.05, 2.1], h = 0.66;
  const ya = 1.1, yb = 1.94, yc = 2.78, yd = 3.6;
  const A0 = node(s, X[0], ya, W[0], h, "전체 진행", "처음 이관하는 프로젝트 (기본 선택)", "done");
  const A1 = node(s, X[1], ya, W[1], h, "일반 프로젝트", "12단계를 모두 거쳐 빌드가 되게 고칩니다", "auto");
  const A2 = node(s, X[2], ya, W[2], h, "취약점 조치", "이관 빌드가 성공하면 이어서 합니다", "scan");
  const A3 = node(s, X[3], ya, W[3], h, "받는 것", "보고서, 변환된 프로젝트, 취약점 조치 결과", "ask");
  const B1 = node(s, X[1], yb, W[1] + 0.25, h + 0.04, "사내 프레임워크 프로젝트", "JDK 21과 jakarta만 바꾸고 빌드·수정합니다. 구조 정리와 구조 검토는 하지 않습니다", "auto");
  const C0 = node(s, X[0], yc, W[0], h, "취약점 해결", "이미 이관한 프로젝트를 다시 점검할 때", "done");
  const C1 = node(s, X[1], yc, W[1], h, "이관 없이", "올린 프로젝트를 그대로 빌드합니다", "auto");
  const C2 = node(s, X[2], yc, W[2], h, "취약점 조치", "검사하고 라이브러리 버전을 올립니다", "scan");
  const C3 = node(s, X[3], yc, W[3], h, "받는 것", "취약점 조치 보고서와 결과 ZIP", "ask");
  const hz = (p, q) => seg(s, p.r, p.cy, q.l, q.cy, { head: true });
  hz(A0, A1); hz(A1, A2); hz(A2, A3); hz(C0, C1); hz(C1, C2); hz(C2, C3);
  // 분석에서 사내 프레임워크를 찾으면: 전체 진행 → 줄인 범위 → 취약점 조치
  const bx = A0.r - 0.2;
  route(s, [[bx, A0.b], [bx, B1.cy], [B1.l, B1.cy]]);
  para(s, "분석에서 사내 프레임워크가 보이면 자동으로", X[0], A0.b + 0.12, bx - X[0] - 0.08, 0.5, { size: 8.5, italic: true, color: "4A4F4A", align: "right" });
  route(s, [[B1.r, B1.cy], [A2.cx, B1.cy], [A2.cx, A2.b]]);
  // 받지 않는 프로젝트
  para(s, "받지 않는 프로젝트", X[0], yd + 0.04, W[0], 0.26, { size: 11, bold: true, color: "1C2A1E" });
  para(s, "처음 단계에서 멈추고, 화면과 보고서에 이유가 나옵니다", X[0], yd + 0.3, W[0], 0.42, { size: 8.5, color: "4A4F4A" });
  node(s, X[1], yd, W[1], 0.62, "Ant 프로젝트", "「Maven 변환 후 신청 부탁드립니다.」", "loop", { titleSize: 10.5 });
  node(s, X[2], yd, W[2], 0.62, "Gradle, 또는 pom.xml 없음", "Maven 프로젝트만 받습니다", "loop", { titleSize: 10.5 });
  node(s, X[3], yd, W[3], 0.62, "Oracle ADF 프로젝트", "분석에서 멈춥니다. 「ADF 프로젝트라 변환이 불가능합니다」", "loop", { titleSize: 10.5 });
}

// ---------------------------------------------------------------- 수정 루프 그림들
function drawLoop(s) {
  const w = 1.6, h = 0.85, gx = 0.25, y = 1.45;
  const col = i => 0.5 + i * (w + gx);
  const b = node(s, col(0), y, w, h, "빌드", "남은 오류를 봅니다", "build");
  const sc = node(s, col(1), y, w, h, "판정", "직전 수정이 도움이 됐나? 나빠졌으면 되돌림", "auto");
  const t = node(s, col(2), y, w, h, "오류 묶기", "같은 원인끼리 한 묶음, 근본 원인부터", "auto");
  const q = node(s, col(3), y, w, h, "방금 손댄 파일?", "그렇다면 다음 빌드까지 보류", "ask");
  const f = node(s, col(4), y, w, h, "고치기", "LLM 없이 먼저, 안 되면 LLM에게 요청", "loop");
  const ok = node(s, col(0), y + 1.25, w, h, "최종 빌드로", "더 고칠 것이 없거나 한도에 닿았습니다", "done");
  const hold = node(s, col(3), y + 1.25, w, h, "보류", "다음 빌드 뒤에 다시 봅니다", "auto");
  const hz = (p, q2) => seg(s, p.r, p.cy, q2.l, q2.cy, { head: true });
  hz(b, sc); hz(sc, t); hz(t, q); hz(q, f); label(s, (q.r + f.l) / 2, q.t - 0.12, "아니오", { w: 0.5 });
  seg(s, q.cx, q.b, hold.cx, hold.t, { head: true }); label(s, q.cx + 0.25, (q.b + hold.t) / 2, "예", { w: 0.3 });
  seg(s, b.cx, b.b, ok.cx, ok.t, { head: true }); label(s, b.cx + 0.45, (b.b + ok.t) / 2, "성공 또는 한도", { w: 0.85 });
  const top = y - 0.25;
  route(s, [[f.cx, f.t], [f.cx, top], [b.cx, top], [b.cx, b.t]]);
  label(s, (f.cx + b.cx) / 2, top, "다음 바퀴, 기본 50바퀴까지", { w: 1.7 });
}
function drawLadder(s) {
  const w = 1.6, h = 0.76, gx = 0.25, ys = [1.05, 1.98, 2.91];
  const col = i => 0.5 + i * (w + gx);
  const q = node(s, col(0), ys[1], w, h, "저장된 방법이 있나?", "지식베이스에서 같은 오류 묶음을 찾습니다", "ask");
  const r = node(s, col(1), ys[0], w, h, "저장된 방법 적용", "다른 프로젝트에서 통한 스크립트를 LLM 없이", "auto");
  const ok = node(s, col(2), ys[0], w, h, "고쳐지면 해결", "그 방법의 성공 기록이 늘어납니다", "done");
  const l = node(s, col(2), ys[1], w, h, "LLM에게 요청", "오류, 파일, pom.xml, 버전 정보와 실패 이유까지", "llm");
  const c = node(s, col(3), ys[1], w, h, "빌드로 확인", "고쳐졌나? 안 되면 보강해 다시", "ask");
  const st = node(s, col(4), ys[0], w, h, "해결, 방법 저장", "다음 프로젝트가 다시 씁니다", "done");
  const ho = node(s, col(4), ys[2], w, h, "담당 팀 몫으로", "보고서 「진행하지 않은 항목」에 남깁니다", "loop");
  route(s, [[q.cx, q.t], [q.cx, r.cy], [r.l, r.cy]]); label(s, q.cx + 0.35, (q.t + r.cy) / 2, "있음", { w: 0.45 });
  seg(s, q.r, q.cy, l.l, l.cy, { head: true }); label(s, (q.r + l.l) / 2, q.cy + 0.22, "없음", { w: 0.45 });
  seg(s, r.r, r.cy, ok.l, ok.cy, { head: true });
  route(s, [[r.cx, r.b], [r.cx, l.cy - 0.2], [l.l, l.cy - 0.2]]); label(s, r.cx + 0.4, (r.b + l.cy) / 2, "안 통함", { w: 0.6 });
  seg(s, l.r, l.cy, c.l, c.cy, { head: true });
  route(s, [[c.cx, c.t], [c.cx, st.cy], [st.l, st.cy]]); label(s, c.cx + 0.4, (c.t + st.cy) / 2, "고쳐짐", { w: 0.55 });
  route(s, [[c.cx, c.b], [c.cx, ho.cy], [ho.l, ho.cy]]); label(s, c.cx + 0.45, (c.b + ho.cy) / 2, "한도 소진", { w: 0.65 });
  const under = l.b + 0.3;
  route(s, [[c.cx - 0.5, c.b], [c.cx - 0.5, under], [l.cx, under], [l.cx, l.b]]); label(s, (c.cx - 0.5 + l.cx) / 2, under, "다시", { w: 0.4 });
}
function drawKB(s) {
  const p1 = node(s, 0.5, 1.1, 2.5, 0.8, "프로젝트 1", "처음 보는 오류. LLM이 고치고 그 방법을 저장합니다", "auto");
  const hu = node(s, 0.5, 2.6, 2.5, 0.9, "운영 담당자", "잘못 저장된 방법은 쓰지 않게 끄고, 손대지 말아야 할 오류는 표시해 둡니다", "llm");
  const kb = node(s, 3.6, 1.75, 2.8, 1.05, "지식베이스", "오류 묶음마다 고친 스크립트, 성공·실패 기록, 실패 이유, 고치기 전·후 코드", "done", { titleSize: 13, descSize: 9 });
  const p2 = node(s, 7.0, 1.1, 2.5, 0.8, "프로젝트 2", "같은 오류. 저장된 방법으로 LLM 없이 바로 해결", "auto");
  const p3 = node(s, 7.0, 2.6, 2.5, 0.9, "프로젝트 3 …", "안 통하면 보강해서 새 버전으로 저장. 갈수록 LLM을 덜 부릅니다", "auto");
  seg(s, p1.r, p1.cy, kb.l, kb.cy - 0.2, { head: true });
  seg(s, hu.r, hu.cy, kb.l, kb.cy + 0.2, { head: true, dash: true });
  seg(s, kb.r, kb.cy - 0.2, p2.l, p2.cy, { head: true, tail: true });
  seg(s, kb.r, kb.cy + 0.2, p3.l, p3.cy, { head: true, tail: true });
}
// 도형 흐름도 슬라이드: 위에 도형, 아래 두세 줄
function drawnIn(section, title, draw, lines, notes, o = {}) {
  const s = content(title, section, notes);
  draw(s);
  const lh = o.linesH || 1.0;
  bullets(s, lines, 0.5, 5.15 - lh, 9, lh, o.size || 11.5);
  return s;
}

// ---------------------------------------------------------------- 보고서 읽는 법: 절 이름 칸 + 여기에 있는 것 + 이렇게 하시면 됩니다
function readingRows(s, rows, o = {}) {
  const x0 = 0.5, wb = 2.05, x1 = 2.7, w1 = 3.55, x2 = 6.4, w2 = 3.1;
  const top = o.top || 1.1, pitch = o.pitch || 0.37, bh = pitch - 0.05;
  para(s, "여기에 있는 것", x1, top, w1, 0.24, { size: 10, bold: true, color: "2C5F2D" });
  para(s, "이렇게 하시면 됩니다", x2, top, w2, 0.24, { size: 10, bold: true, color: "2C5F2D" });
  rows.forEach(([name, kind, what, todo], i) => {
    const y = top + 0.28 + i * pitch;
    const k = KIND[kind];
    s.addText(name, { shape: pres.shapes.ROUNDED_RECTANGLE, rectRadius: 0.05, x: x0, y, w: wb, h: bh, fill: { color: k.fill }, line: { color: k.line, width: 1 },
      fontSize: 10, bold: true, color: k.text, align: "center", valign: "middle", margin: 0.03, fontFace: THEME.bodyFontFace });
    para(s, what, x1, y, w1, bh, { size: 9.5, valign: "middle" });
    para(s, todo, x2, y, w2, bh, { size: 9.5, valign: "middle", color: "2C3A2E" });
    if (i < rows.length - 1) s.addShape(pres.shapes.LINE, { x: x1, y: y + bh + 0.025, w: x2 + w2 - x1, h: 0, line: { color: "D5DBD3", width: 0.5 } });
  });
}

// ONLY=intake,analyze 를 주면 그 단계 쪽만 만든다(그림을 고칠 때 빨리 보려고).
const ONLY = (process.env.ONLY || "").split(",").map(v => v.trim()).filter(Boolean);
function buildAll() {
pres.addSection({ title: "흐름" });
{ const s = pres.addSlide({ masterName: "TITLE", sectionTitle: "흐름" });
  s.addText("마이그레이션 에이전트, 이렇게 돌아갑니다", { placeholder: "title" });
  s.addText("프로젝트를 올리면 무슨 일이 일어나는지, 무엇을 받고 어떻게 보면 되는지", { placeholder: "body" });
  s.addNotes("프로젝트를 올려 주시는 팀을 위한 안내입니다. 프로젝트 하나가 들어와서 결과가 나올 때까지 어떤 단계를 거치는지, 끝나면 무엇을 받는지, 받은 보고서를 어떻게 읽고 무엇을 하시면 되는지를 순서대로 설명드립니다. " +
    "설정이나 내부 구조 이야기는 최대한 뺐습니다. 궁금한 점이 있으면 보고서 맨 위의 결과 줄(SBMIG-RESULT로 시작하는 한 줄)을 함께 보내 주시면 빨리 확인해 드릴 수 있습니다."); }

{ const s = content("전체 흐름", "흐름",
    "프로젝트 하나는 이 열두 단계를 차례로 거칩니다. 위의 두 줄은 왼쪽에서 오른쪽으로, 맨 아래 줄은 오른쪽에서 왼쪽으로 읽으시면 됩니다.\n\n" +
    "①에서 ⑦까지는 프로젝트를 새 버전에 맞게 바꾸는 단계입니다. 회사 표준 구조로 바꾸고, LLM이 pom.xml을 한 번 점검합니다. ⑧에서 처음으로 전체를 빌드해 보고, 성공하면 바로 취약점 조치로 넘어갑니다. 실패하면 ⑨ 수정 루프에서 오류를 하나씩 고치며 빌드를 반복하고, ⑩ 최종 빌드로 결과를 확정합니다.\n\n" +
    "⑪ 취약점 조치는 최종 빌드가 성공한 프로젝트만 합니다. 회사 취약점 점검 도구인 IVAS로 검사하고, 취약한 라이브러리의 버전을 올려 다시 빌드하고 다시 검사합니다. 빌드가 실패한 프로젝트는 취약점 조치 없이 바로 마무리로 갑니다. 취약점 조치 결과는 이관 결과를 바꾸지 않습니다.\n\n" +
    "⑫ 마무리에서는 보고서와 결과 파일을 만들고, 이번에 고친 방법을 지식베이스에 남겨 다음 프로젝트가 다시 쓰게 합니다. 보고서에는 취약점 조치 요약도 들어가기 때문에 보고서와 결과 파일은 취약점 조치까지 끝난 뒤에 내려받을 수 있습니다.\n\n" +
    "LLM이 돕는 곳은 ⑦ 구조 검토와 ⑨ 수정 루프 두 곳뿐입니다. 나머지는 정해진 규칙대로 진행하고, 취약점 조치에도 LLM은 쓰지 않습니다. 사내 프레임워크를 쓰는 프로젝트는 ⑥ 구조 정리와 ⑦ 구조 검토를 건너뛰는데, 이 내용은 다음 장에서 설명드립니다.");
  drawOverview(s);
  legend(s, 4.38);
  para(s, "LLM이 돕는 곳은 ⑦과 ⑨ 두 곳뿐이고, 고쳐졌는지는 언제나 다음 빌드 결과로 판단합니다.", 0.5, 4.75, 9, 0.3, { size: 11, italic: true }); }

{ const s = content("프로젝트에 따라 길이 달라집니다", "흐름",
    "프로젝트를 올릴 때 화면에서 전체 진행과 취약점 해결 중 하나를 고릅니다. 처음 이관하는 프로젝트라면 기본값인 전체 진행을 그대로 두시면 됩니다. 이관을 하고, 빌드가 성공하면 같은 자리에서 취약점 조치까지 이어서 합니다. 받는 것은 이관 보고서(report.md), 변환된 프로젝트(result.zip), 그리고 화면 아래 취약점 조치 영역의 보고서와 결과 ZIP입니다.\n\n" +
    "취약점 해결은 이미 이관을 마친 프로젝트를 나중에 다시 점검할 때 고릅니다. 이관 단계는 하나도 거치지 않고, 올린 프로젝트를 그대로 빌드해서 검사하고 조치합니다. LLM도 쓰지 않습니다. 올린 그대로 빌드가 되지 않는 프로젝트는 검사하지 않으니, 빌드되는 상태로 올려 주셔야 합니다.\n\n" +
    "사내 프레임워크를 쓰는 프로젝트는 따로 고르실 필요가 없습니다. 에이전트가 분석 단계에서 pom.xml에 직접 적힌 경우뿐 아니라 부모 pom을 통해 들어오거나, 프로젝트 안에 jar로 들어 있거나, 다른 라이브러리를 거쳐 들어오는 경우까지 찾아냅니다. 이런 프로젝트는 JDK 21과 jakarta 전환만 하고, 빌드 오류를 고친 뒤 취약점 조치까지 합니다. 회사 표준 구조로 바꾸는 구조 정리와 구조 검토는 하지 않고, 화면에도 이 두 단계가 '하지 않음'으로 표시됩니다. 프레임워크 버전은 그대로 두고, 프레임워크에서 들어온 취약점은 사내 라이브러리 항목으로 따로 알려 드립니다.\n\n" +
    "받지 않는 프로젝트도 있습니다. Ant 프로젝트는 Maven으로 바꾼 뒤 다시 신청해 달라는 안내와 함께 접수에서 멈춥니다. Gradle 프로젝트와 pom.xml이 없는 폴더도 Maven 프로젝트만 받는다는 안내와 함께 멈춥니다. Oracle ADF 프로젝트는 분석에서 ADF의 흔적이 두 종류 이상 보이면 변환할 수 없다고 알리고 멈춥니다. 한 종류만 보이면 변환은 계속하고, 보고서에서 ADF 프로젝트인지 확인해 달라고 말씀드립니다. 어느 경우든 화면과 보고서 첫머리에 이유가 나옵니다.");
  drawRoads(s);
  bullets(s, [
    "처음 이관하는 프로젝트는 전체 진행을, 이미 이관한 프로젝트를 다시 점검할 때는 취약점 해결을 고르시면 됩니다.",
    "사내 프레임워크를 쓰는 프로젝트는 에이전트가 알아서 범위를 줄입니다. JDK 21과 jakarta만 바꾸고, 프레임워크 버전은 그대로 둡니다.",
    "받지 않는 프로젝트는 처음 단계에서 멈추고, 화면과 보고서 첫머리에 이유와 다시 신청하는 방법이 나옵니다."], 0.5, 4.3, 9, 0.88, 10.5); }

pres.addSection({ title: "단계별" });
["pod", "intake", "analyze", "plan", "transform", "restructure", "review", "verify"].forEach(k => stepSlide(k, "단계별"));

pres.addSection({ title: "수정 루프" });
{ const s = content("오류는 한 번에 다 보이지 않습니다", "수정 루프",
    "빌드 오류를 고쳐 본 분이라면 익숙하실 겁니다. 오류 몇 개를 고치고 다시 빌드하면 처음에 없던 오류가 새로 나옵니다. 고친 것이 잘못돼서가 아니라, 앞에서 막혀 있던 빌드가 한 걸음 더 나아갔기 때문입니다.\n\n" +
    "Maven은 모듈을 순서대로 빌드하다가 하나가 실패하면 거기서 멈추고, 자바 컴파일러도 문법 오류가 있으면 타입 오류는 알려 주지 않으며 한 번에 100개까지만 보고합니다. 그래서 에이전트는 고칠 때마다 다시 빌드해서 새로 드러난 오류를 이어서 처리합니다. 오류가 보이지 않는다고 해결로 치지 않고, 빌드가 그 오류가 나던 단계까지 갔는데도 사라졌을 때만 해결로 셉니다.");
  para(s, "⑧ 검증 빌드에서 처음으로 전체를 빌드합니다. 이때 보이는 오류가 전부는 아닙니다. 고치고 다시 빌드할 때마다 뒤에 숨어 있던 오류가 차례로 드러납니다.", 0.5, 1.15, 9, 0.7, { size: 12.5 });
  card(s, 0.5, 2.0, 4.4, 3.05, "왜 한 번에 다 안 보이나", [
    "Maven은 모듈을 하나씩 순서대로 빌드하다가 실패하면 거기서 멈춥니다. 뒤 모듈의 오류는 앞 모듈이 통과해야 보입니다.",
    "자바 컴파일러도 단계별로 봅니다. 문법 오류가 있으면 타입 오류는 알려 주지 않고, 한 번에 100개까지만 보고합니다."], { bodySize: 11.5 });
  card(s, 5.1, 2.0, 4.4, 3.05, "에이전트는 이렇게 다룹니다", [
    "오류 하나를 고치면 다른 오류가 나오는 것을 정상으로 봅니다. 고칠 때마다 다시 빌드해서 새로 드러난 오류를 이어서 처리합니다.",
    "오류가 안 보인다고 고쳐진 것으로 치지 않습니다. 빌드가 그 오류가 나던 단계까지 갔을 때만 해결로 셉니다."], { bodySize: 11.5, fill: "EEF3EA" }); }

drawnIn("수정 루프", "검증 빌드와 수정 루프", drawLoop, [
  "⑧ 검증 빌드가 성공하면 바로 다음 단계로 갑니다. 실패하면 ⑨ 수정 루프에서 빌드가 되거나 한도에 닿을 때까지 돕니다.",
  "한 바퀴는 고치기 → 다시 빌드 → 판정입니다. 도움이 되지 않은 수정은 되돌립니다. 기본 50바퀴까지 돕니다.",
  "근본 원인부터 고치므로, 이번 바퀴에 이미 손댄 파일이나 pom.xml에 걸린 오류는 다음 빌드 뒤에 봅니다."],
  "수정 루프 한 바퀴를 보여 드리는 그림입니다. 빌드가 끝나면 먼저 직전에 한 수정이 도움이 됐는지 판정하고, 나빠졌으면 그 수정을 되돌립니다. 그다음 남은 오류를 원인별로 묶습니다. 같은 원인에서 나온 오류 여러 개는 한 묶음으로 한 번에 고칩니다.\n\n" +
  "한 바퀴에 다루는 오류 묶음은 많아야 8개이고, 근본 원인으로 보이는 것부터 처리합니다. 같은 바퀴에서 이미 손댄 파일이나 pom.xml에 걸린 오류는 서로 엇갈리지 않도록 다음 빌드 결과를 보고 나서 다룹니다.\n\n" +
  "루프는 빌드가 성공하거나, 50바퀴에 닿거나, 더 해 볼 방법이 없거나, 한 건에 주어진 시간(4시간)을 넘기면 멈춥니다. 어떤 경우든 마지막에 최종 빌드를 한 번 더 하고, 남은 오류는 보고서에 남깁니다.", { linesH: 1.25, size: 11.5 });

drawnIn("수정 루프", "오류 하나가 고쳐지는 길", drawLadder, [
  "빠진 버전, 프로젝트 안에 든 jar, jakarta 판 라이브러리처럼 정해진 처리가 있는 오류는 먼저 LLM 없이 고쳐 봅니다. 그다음 지식베이스에서 다른 프로젝트가 쓴 방법을 찾습니다.",
  "없거나 통하지 않으면 LLM에게 오류와 관련 파일, pom.xml, 라이브러리 버전 정보를 주고 수정을 받습니다. 실패하면 그 결과를 붙여 보강을 요청합니다.",
  "한 묶음에 LLM을 20번까지 부릅니다. 그래도 안 되면 보고서 「진행하지 않은 항목」에 남기고, 무엇을 시도했는지는 errors.md에 있습니다."],
  "오류 묶음 하나가 어떤 순서로 고쳐지는지 보여 드리는 그림입니다.\n\n" +
  "먼저 LLM 없이 정해진 방법으로 고칠 수 있는 오류인지 봅니다. pom.xml에 버전이 빠진 의존성, 프로젝트 안에 jar 파일로 들어 있는 라이브러리, 리소스 필터링 때문에 깨지는 바이너리 파일, jakarta 판이 따로 있는 라이브러리, 회사 부모 pom 버전 같은 경우입니다.\n\n" +
  "그다음 지식베이스를 봅니다. 다른 프로젝트에서 같은 오류를 고친 스크립트가 있으면 LLM을 부르지 않고 그대로 실행합니다. 없거나 통하지 않으면 LLM에게 요청합니다. 이때 오류 메시지, 오류가 난 파일의 해당 부분, pom.xml 일부, 회사 저장소에서 확인한 라이브러리 버전, 그리고 앞선 시도가 왜 실패했는지를 함께 줍니다. 처음 받은 스크립트가 통하지 않으면 실패 결과를 붙여 보강을 요청하고, 보강을 여러 번 해도 안 되면 처음부터 새로 받습니다.\n\n" +
  "한 묶음에 LLM을 부르는 횟수는 20번까지입니다. 그 안에서 고치지 못하면 더 시도하지 않고, 보고서의 「진행하지 않은 항목」에 남깁니다. 루프가 끝날 때까지 남은 다른 오류는 「해결하지 못한 오류」에 나옵니다. 무엇을 어떻게 시도했는지는 errors.md에 모두 남아 있습니다.", { linesH: 1.25, size: 10.5 });

{ const s = content("고쳐졌는지는 빌드가 판정합니다", "수정 루프",
    "LLM이 고쳤다고 말해도 그대로 믿지 않습니다. 다음 빌드 결과만 봅니다.\n\n" +
    "노리던 오류가 모두 사라지고 빌드가 그 단계까지 갔으면 좋아진 것으로 보고 수정을 유지합니다. 일부만 사라졌으면 사라진 오류는 해결로 적고, 수정은 되돌린 뒤 보강해서 다시 시도합니다. 손댄 파일에 새 오류가 생겼거나 오류 수가 1.5배 넘게 늘었으면 나빠진 것으로 보고 되돌립니다. 다만 그 뒤에 한 수정이 이 수정에 기대고 있으면 함께 깨지지 않도록 남겨 둡니다.\n\n" +
    "빌드가 시간 초과로 끝났거나 그 오류가 나는 단계 전에 멈췄다면 판단할 수 없으니, 지식베이스에 성공이나 실패로 남기지 않고 되돌린 뒤 다시 봅니다.");
  table(s, [["다음 빌드 결과", "뜻", "그 다음"],
    ["좋아짐", "노리던 오류가 모두 사라졌고, 빌드가 그 단계까지 갔습니다", "수정을 유지하고 지식베이스에 성공으로 기록"],
    ["일부 좋아짐", "일부만 사라졌고, 손댄 파일에 새 오류는 없습니다", "사라진 오류는 해결로 적고, 되돌린 뒤 보강해 다시 시도"],
    ["변화 없음", "오류가 그대로입니다", "수정을 되돌리고 다음 방법으로"],
    ["나빠짐", "손댄 파일에 새 오류가 생겼거나 오류가 크게 늘었습니다", "되돌리고 다시 빌드 (뒤의 수정이 기대고 있으면 유지)"],
    ["알 수 없음", "시간 초과이거나 그 오류 단계 전에 멈춰 판단할 수 없습니다", "지식베이스에 남기지 않고, 다음 시도 전에 되돌림"]], 0.5, 1.15, 9, [1.6, 4.2, 3.2], { size: 11 });
  para(s, "'오류가 안 보인다'와 '고쳐졌다'는 다릅니다. 앞 단계에서 빌드가 멈추면 뒤 단계의 오류는 보이지 않을 뿐이므로, 빌드가 그 단계까지 갔을 때만 해결로 셉니다.", 0.5, 3.45, 9, 0.8, { size: 11, italic: true }); }

pres.addSection({ title: "취약점 조치와 마무리" });
stepSlide("remediation", "취약점 조치와 마무리");
stepSlide("finalize", "취약점 조치와 마무리");

pres.addSection({ title: "결과 읽기" });
{ const s = content("보고서는 이렇게 읽습니다", "결과 읽기",
    "이관 보고서 report.md는 마이그레이션을 요청하신 분께 드리는 안내서입니다. 위에서부터 이 순서로 나오고, 내용이 없는 절은 아예 나오지 않습니다.\n\n" +
    "맨 위 표에서 결과를 먼저 보세요. 빌드 성공, 빌드 실패, 실행 중단 가운데 하나가 적혀 있습니다. 빌드가 실패했다면 표 바로 아래 문장이 어느 절부터 보면 되는지 알려 드립니다.\n\n" +
    "가장 중요한 절은 「확인해 주세요」입니다. 지원이 끝난 라이브러리처럼 담당 팀이 챙겨야 할 일을 한 목록으로 모았습니다. 「코드 수정 내역」에는 자동 변환 뒤에 에이전트가 직접 고친 파일마다 최종 diff를 보여 드립니다. 여러 번 고쳤더라도 파일마다 한 번, 최종 모습만 나옵니다.\n\n" +
    "보고서에는 해결된 오류 목록을 싣지 않았습니다. 남은 오류만 「해결하지 못한 오류」에 나오고, 오류마다 어떤 시도를 했는지는 함께 받으시는 errors.md에 모두 남아 있습니다. 전체 변경은 changes.patch에 있습니다.");
  readingRows(s, [
    ["머리 표", "auto", "결과(빌드 성공 · 빌드 실패 · 실행 중단), 전환 후 버전, 결과 한 줄", "결과부터 봅니다. 문의할 때는 결과 한 줄을 함께 보내 주세요"],
    ["전환 내용", "auto", "Java, Spring Boot, 부모 pom, 의존성 버전이 어떻게 바뀌었는지", "⚠ 표시한 의존성은 기존 버전을 그대로 둔 것이니 동작을 확인합니다"],
    ["확인해 주세요", "done", "지원이 끝난 라이브러리와 담당 팀이 할 일을 한 목록으로", "위에서부터 차례로 처리합니다"],
    ["코드 수정 내역", "auto", "자동 변환 요약, 그 뒤 직접 고친 파일마다 최종 diff", "고친 코드를 검토합니다. 전체 변경은 changes.patch에 있습니다"],
    ["해결하지 못한 오류", "loop", "최종 빌드에 남은 오류를 파일과 줄 번호로", "직접 고칩니다. 시도한 기록은 errors.md에 있습니다"],
    ["진행하지 않은 항목", "loop", "프로젝트 안에서 해결할 수 없어 손대지 않은 것", "라이브러리 담당자나 환경 쪽에 요청합니다"],
    ["취약점 조치", "scan", "이어서 한 취약점 조치의 결과와 숫자", "자세한 내용은 취약점 조치 보고서에서 봅니다"],
    ["참고", "ask", "실행 정보, LLM 사용량, 산출물 목록 (접혀 있음)", "필요할 때만 펼쳐 봅니다"],
  ], { top: 1.08, pitch: 0.37 });
  bullets(s, [
    "「확인해 주세요」가 담당 팀이 할 일의 목록입니다. 빌드가 성공했어도 여기 적힌 것은 꼭 확인해 주세요.",
    "해결된 오류는 보고서에 싣지 않았습니다. 오류마다 무엇을 시도했는지는 errors.md에, 전체 변경은 changes.patch에 있습니다."], 0.5, 4.38, 9, 0.75, 10.5); }

{ const s = content("취약점 조치 결과는 이렇게 봅니다", "결과 읽기",
    "취약점 조치는 이관 보고서와 별도로 자기 보고서와 결과 ZIP을 만듭니다. 화면 아래 취약점 조치 영역에서 조치 결과 보고서와 결과 ZIP을 받으실 수 있습니다. 이관 결과 버튼의 result.zip은 이관만 한 프로젝트이고, 취약점 조치까지 반영된 프로젝트는 취약점 조치 영역의 결과 ZIP이니 헷갈리지 않도록 주의해 주세요.\n\n" +
    "보고서 맨 위의 한 문장과 「해야 할 일」만 보셔도 판단이 서도록 썼습니다. 조치를 마쳤으면 결과 ZIP을 받아 쓰시면 되고, 일부만 조치됐으면 「해야 할 일」을 위에서부터 처리하시면 됩니다.\n\n" +
    "위험도는 IVAS 점수로 표시합니다. 9.0 이상은 빨강(심각), 7.0 이상은 주황(높음), 4.0 이상은 노랑(보통)이고, 위험한 줄이 표의 위에 옵니다.\n\n" +
    "사내 라이브러리에서 들어온 취약점은 프로젝트에서 고칠 수 없어 손대지 않습니다. 해당 아티팩트는 예외 처리가 가능하니 그 담당자에게 연락해 주시면 됩니다. 「직접 확인이 필요한 취약점」에는 라이브러리가 어느 pom.xml에서 어떤 경로로 들어왔는지 '들어온 경로'로 적어 두었습니다.\n\n" +
    "결과 ZIP 옆에 '빌드 실패본'이라고 표시돼 있으면, 버전을 올린 수정본이 빌드에 실패했다는 뜻입니다. 그 ZIP은 배포하지 말고 보고서의 주의 문단과 빌드 로그를 먼저 확인해 주세요. 앞 회차에서 검증을 마친 결과가 있으면 따로 받을 수 있습니다.");
  readingRows(s, [
    ["결과 문장", "scan", "✅ 조치를 마쳤습니다 · ⚠️ 일부만 조치되었습니다 · ❌ 조치하지 못했습니다 · ⏸ 확인이 필요합니다", "이 문장과 「해야 할 일」만 봐도 판단이 섭니다"],
    ["요약", "auto", "조치 전후 취약점 수와 위험도별 숫자 (빨강 심각 · 주황 높음 · 노랑 보통)", "얼마나 줄었는지 확인합니다"],
    ["해야 할 일", "done", "지금 하실 일만 번호를 붙여서", "위에서부터 차례로 처리합니다"],
    ["조치 완료", "auto", "올린 버전과 해결된 취약점. 「주의」 칸은 메이저 버전 변경처럼 큰 변경", "결과 ZIP을 받고, 「주의」가 붙은 변경은 동작을 확인합니다"],
    ["사내 라이브러리에서 발생한 취약점", "loop", "사내 아티팩트에서 들어온 취약점을 아티팩트별로", "그 아티팩트 담당자에게 연락합니다 (예외 처리 가능)"],
    ["직접 확인이 필요한 취약점", "loop", "자동으로 바꿀 수 없는 항목을 원인별로, 어느 의존성을 거쳐 들어왔는지와 함께", "그 의존성을 선언한 pom.xml에서 담당 팀이 조치합니다"],
    ["변경하지 않은 것 · 처리 기록", "ask", "소스와 구조는 그대로라는 확인, 처리 과정 (접혀 있음)", "문의할 때 처리 기록의 내용을 함께 보내 주세요"],
  ], { top: 1.08, pitch: 0.4 });
  bullets(s, [
    "취약점 조치까지 반영된 프로젝트는 취약점 조치 영역의 결과 ZIP입니다. 이관 결과의 result.zip에는 조치가 들어 있지 않습니다.",
    "결과 ZIP에 '빌드 실패본' 표시가 있으면 배포하지 말고 보고서의 주의 문단과 빌드 로그부터 확인해 주세요."], 0.5, 4.38, 9, 0.75, 10.5); }

pres.addSection({ title: "지식베이스" });
drawnIn("지식베이스", "한 번 고친 방법은 다음 프로젝트에서 다시 씁니다", drawKB, [
  "같은 모양의 오류는 어느 프로젝트에서 나와도 같은 묶음으로 모입니다. 묶음마다 고친 스크립트와 성공·실패 기록, 실패한 이유, 고치기 전·후 코드가 쌓입니다.",
  "프로젝트가 쌓일수록 저장된 방법으로 바로 고치는 경우가 늘어 LLM을 덜 부릅니다. 끝나면 보고서 「확인해 주세요」에 담당 팀이 확인할 일이 정리되어 있습니다.",
  "운영 담당자가 잘못 저장된 방법을 끄거나, 손대지 말아야 할 오류를 표시해 둘 수 있습니다."],
  "에이전트가 프로젝트를 거듭할수록 나아지는 이유가 이 지식베이스입니다. 같은 모양의 오류는 어느 팀의 프로젝트에서 나와도 같은 묶음으로 모이고, 묶음마다 그 오류를 고친 스크립트가 쌓입니다. 다음 프로젝트에서 같은 오류가 나오면 LLM을 부르지 않고 저장된 스크립트를 먼저 실행합니다. 통하면 성공 기록이 늘고, 통하지 않으면 보강해서 새 버전으로 저장합니다.\n\n" +
  "스크립트가 무엇을 바꾸는지도 남기 때문에, 운영 담당자가 문제 있는 방법을 찾아 쓰지 않게 끌 수 있습니다. 반대로 자동으로 손대면 안 되는 오류는 고치지 말라고 표시해 두면, 그 오류는 시도하지 않고 보고서에 이유와 함께 남습니다.\n\n" +
  "지식베이스는 데이터베이스가 아닌 파일 폴더라서, 복사해서 다른 곳으로 옮길 수도 있습니다.", { linesH: 1.45, size: 11 });
}
if (ONLY.length) { pres.addSection({ title: "단계별" }); ONLY.forEach(k => stepSlide(k, "단계별")); } else buildAll();

(async () => { await pres.writeFile({ fileName: OUT }); if (applyTheme) await applyTheme(OUT, THEME); console.log("written", OUT); })().catch(e => { console.error(e); process.exit(1); });
