// migration agent — 담당자용 흐름 안내 덱 (2026-10-29): 전체 흐름도 한 장 + 단계별 설명 + 수정 루프.
// Run from scripts/handbook:  NODE_PATH=$PWD/node_modules APPLY_THEME_JS=<pptx skill>/scripts/apply_theme.js node build_migration_agent_deck.js ../../docs/migration-agent/migration-agent.pptx
// 흐름도는 전부 파워포인트 도형(둥근 네모 + 선)으로 그린다 — 파워포인트에서 글자·칸을 바로 고칠 수 있다. 그림 파일을 쓰지 않는다.
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
// ---------------------------------------------------------------- 도형으로 그리는 흐름도 (파워포인트에서 편집 가능)
const KIND = { auto: { fill: "EEF3EA", line: "2C5F2D", text: "1C2A1E" }, build: { fill: "F2CC8F", line: "C9A24C", text: "1C2A1E" },
               llm: { fill: "3D5A80", line: "3D5A80", text: "FFFFFF" }, loop: { fill: "E07A5F", line: "E07A5F", text: "FFFFFF" },
               done: { fill: "2C5F2D", line: "2C5F2D", text: "FFFFFF" }, ask: { fill: "FFFFFF", line: "8A8F8A", text: "1C2A1E" } };
const ARROW = "4A4F4A";
// 네모칸 하나 = 도형 + 글(굵은 제목, 작은 설명). 가운데 좌표를 돌려줘서 화살표를 잇는다.
function node(s, x, y, w, h, title, desc, kind = "auto", o = {}) {
  const k = KIND[kind];
  s.addText([{ text: title, options: { bold: true, fontSize: o.titleSize || 11, color: k.text, breakLine: true } },
             { text: desc, options: { fontSize: o.descSize || 8.5, color: k.text } }],
    { shape: pres.shapes.ROUNDED_RECTANGLE, rectRadius: 0.05, x, y, w, h, fill: { color: k.fill }, line: { color: k.line, width: 1 },
      align: "center", valign: "middle", margin: 0.05, paraSpaceAfter: 2, fontFace: THEME.bodyFontFace });
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

// 1. 전체 흐름 — 3줄 (4 · 3 · 4)
function drawOverview(s) {
  const w = 2.0, h = 0.78, gx = 0.333, ys = [1.08, 2.18, 3.28];
  const col = i => 0.5 + i * (w + gx);
  const n = {};
  n.a = node(s, col(0), ys[0], w, h, "① 접수", "작업용 사본을 만듭니다. Ant 프로젝트면 pom.xml 을 만들어 받습니다", "auto");
  n.b = node(s, col(1), ys[0], w, h, "② 분석", "프로젝트 종류와 쓰는 라이브러리를 읽어 둡니다", "auto");
  n.c = node(s, col(2), ys[0], w, h, "③ 계획", "종류에 맞춰 돌릴 단계와 순서를 정합니다", "auto");
  n.e = node(s, col(3), ys[0], w, h, "④ 자동 변환", "JDK 21, jakarta, Spring Boot 4 규칙을 한 번에 적용합니다", "auto");
  n.f = node(s, col(0), ys[1], w, h, "⑤ 1차 컴파일", "지금 보이는 오류를 전부 목록에 적어 둡니다", "build");
  n.g = node(s, col(1), ys[1], w, h, "⑥ 구조 정리", "회사 표준 구조로 모듈과 pom.xml 을 다시 짭니다", "auto");
  n.h = node(s, col(2), ys[1], w, h, "⑦ 구조 검토", "LLM 이 회사 규칙에 맞는지 보고 꼭 필요한 것만 고칩니다", "llm");
  n.i = node(s, col(0), ys[2], w, h, "⑧ 검증 빌드", "mvn clean package. 성공이면 바로 마무리로", "build");
  n.j = node(s, col(1), ys[2], w, h, "⑨ 수정 루프", "오류를 묶어 하나씩 고치고 다시 빌드. 될 때까지 반복", "loop");
  n.k = node(s, col(2), ys[2], w, h, "⑩ 최종 빌드", "한 번 더 전체 빌드로 결과를 확정합니다", "build");
  n.l = node(s, col(3), ys[2], w, h, "⑪ 마무리", "보고서를 쓰고 수정 방법을 지식베이스에 저장합니다", "done");
  const hz = (p, q) => seg(s, p.r, p.cy, q.l, q.cy, { head: true });
  hz(n.a, n.b); hz(n.b, n.c); hz(n.c, n.e); hz(n.f, n.g); hz(n.g, n.h); hz(n.j, n.k); hz(n.k, n.l);
  // 줄 바꿈: ④ → ⑤, ⑦ → ⑧ (아래로 내려와 왼쪽 끝으로)
  const gap1 = (n.e.b + n.f.t) / 2, gap2 = (n.h.b + n.i.t) / 2;
  route(s, [[n.e.cx, n.e.b], [n.e.cx, gap1], [n.f.cx, gap1], [n.f.cx, n.f.t]]);
  route(s, [[n.h.cx, n.h.b], [n.h.cx, gap2], [n.i.cx, gap2], [n.i.cx, n.i.t]]);
  // ⑧ 실패 → ⑨, ⑧ 성공 → ⑪ (아래로 돌아서)
  hz(n.i, n.j); label(s, (n.i.r + n.j.l) / 2, n.i.cy - 0.2, "실패", { w: 0.5 });
  const below = n.i.b + 0.22;
  route(s, [[n.i.cx + 0.5, n.i.b], [n.i.cx + 0.5, below], [n.l.cx, below], [n.l.cx, n.l.b]]);
  label(s, (n.i.cx + n.l.cx) / 2, below, "성공", { w: 0.5 });
}

// 2. 수정 루프 한 바퀴
function drawLoop(s) {
  const w = 1.6, h = 0.85, gx = 0.25, y = 1.45;
  const col = i => 0.5 + i * (w + gx);
  const b = node(s, col(0), y, w, h, "빌드", "남은 오류를 봅니다", "build");
  const sc = node(s, col(1), y, w, h, "채점", "직전 수정이 효과 있었나? 나빠졌으면 되돌림", "auto");
  const t = node(s, col(2), y, w, h, "오류 묶기", "같은 원인끼리 케이스로, 근본 원인부터", "auto");
  const q = node(s, col(3), y, w, h, "방금 손댄 파일?", "그러면 다음 빌드까지 보류", "ask");
  const f = node(s, col(4), y, w, h, "고치기", "저장된 방법 먼저, 없으면 LLM 에게 요청", "loop");
  const ok = node(s, col(0), y + 1.25, w, h, "최종 빌드로", "더 고칠 것이 없습니다", "done");
  const hold = node(s, col(3), y + 1.25, w, h, "보류", "다음 빌드 뒤에 다시 봅니다", "auto");
  const hz = (p, q2) => seg(s, p.r, p.cy, q2.l, q2.cy, { head: true });
  hz(b, sc); hz(sc, t); hz(t, q); hz(q, f); label(s, (q.r + f.l) / 2, q.cy - 0.2, "아니오", { w: 0.5 });
  seg(s, q.cx, q.b, hold.cx, hold.t, { head: true }); label(s, q.cx + 0.25, (q.b + hold.t) / 2, "예", { w: 0.3 });
  seg(s, b.cx, b.b, ok.cx, ok.t, { head: true }); label(s, b.cx + 0.45, (b.b + ok.t) / 2, "빌드 성공", { w: 0.7 });
  const top = y - 0.25;
  route(s, [[f.cx, f.t], [f.cx, top], [b.cx, top], [b.cx, b.t]]);
  label(s, (f.cx + b.cx) / 2, top, "다음 바퀴, 최대 50번", { w: 1.5 });
}

// 3. 오류 하나가 고쳐지는 길
function drawLadder(s) {
  const w = 1.6, h = 0.8, gx = 0.25, ys = [1.1, 2.1, 3.1];
  const col = i => 0.5 + i * (w + gx);
  const q = node(s, col(0), ys[1], w, h, "저장된 방법이 있나?", "지식베이스에서 같은 케이스를 찾습니다", "ask");
  const r = node(s, col(1), ys[0], w, h, "저장된 방법 적용", "앞 프로젝트의 스크립트 그대로, LLM 없이", "auto");
  const ok = node(s, col(2), ys[0], w, h, "해결", "성공 횟수 +1", "done");
  const l = node(s, col(2), ys[1], w, h, "LLM 에게 요청", "오류·파일·pom·버전 정보, 실패 이유까지 줍니다", "llm");
  const c = node(s, col(3), ys[1], w, h, "빌드로 확인", "고쳐졌나? 한 케이스에 최대 3번", "ask");
  const st = node(s, col(4), ys[0], w, h, "해결하고 저장", "새 스크립트를 케이스 폴더에 저장", "done");
  const ho = node(s, col(4), ys[2], w, h, "사람에게 인계", "보고서 맨 앞에 적어 넘깁니다", "loop");
  // 있음: Q 위로 올라가 R 로
  route(s, [[q.cx, q.t], [q.cx, r.cy], [r.l, r.cy]]); label(s, q.cx + 0.35, (q.t + r.cy) / 2, "있음", { w: 0.45 });
  seg(s, q.r, q.cy, l.l, l.cy, { head: true }); label(s, (q.r + l.l) / 2, q.cy + 0.22, "없음", { w: 0.45 });
  seg(s, r.r, r.cy, ok.l, ok.cy, { head: true }); label(s, (r.r + ok.l) / 2, r.cy - 0.2, "고쳐짐", { w: 0.55 });
  route(s, [[r.cx, r.b], [r.cx, l.cy - 0.2], [l.l, l.cy - 0.2]]); label(s, r.cx + 0.4, (r.b + l.cy) / 2, "안 통함", { w: 0.6 });
  seg(s, l.r, l.cy, c.l, c.cy, { head: true });
  route(s, [[c.cx, c.t], [c.cx, st.cy], [st.l, st.cy]]); label(s, c.cx + 0.4, (c.t + st.cy) / 2, "고쳐짐", { w: 0.55 });
  route(s, [[c.cx, c.b], [c.cx, ho.cy], [ho.l, ho.cy]]); label(s, c.cx + 0.45, (c.b + ho.cy) / 2, "3번 실패", { w: 0.65 });
  const under = l.b + 0.3;
  route(s, [[c.cx - 0.5, c.b], [c.cx - 0.5, under], [l.cx, under], [l.cx, l.b]]); label(s, (c.cx - 0.5 + l.cx) / 2, under, "다시", { w: 0.4 });
}

// 4. 지식베이스 순환
function drawKB(s) {
  const p1 = node(s, 0.5, 1.1, 2.5, 0.8, "프로젝트 1", "처음 보는 오류, LLM 이 고치고 저장", "auto");
  const hu = node(s, 0.5, 2.6, 2.5, 0.9, "사람", "버전 표를 적어 주거나, 잘못 배운 케이스를 지우거나, 고치지 말라고 표시", "llm");
  const kb = node(s, 3.6, 1.75, 2.8, 1.05, "지식베이스", "케이스마다 스크립트, 성공·실패 기록, 실패 이유, 고치기 전·후 코드", "done", { titleSize: 13, descSize: 9 });
  const p2 = node(s, 7.0, 1.1, 2.5, 0.8, "프로젝트 2", "같은 오류, 저장된 스크립트로 바로 해결", "auto");
  const p3 = node(s, 7.0, 2.6, 2.5, 0.9, "프로젝트 3 …", "안 통하면 보강해서 새 버전으로. 돌수록 LLM 호출이 줄어듭니다", "auto");
  seg(s, p1.r, p1.cy, kb.l, kb.cy - 0.2, { head: true });
  seg(s, hu.r, hu.cy, kb.l, kb.cy + 0.2, { head: true, dash: true });
  seg(s, kb.r, kb.cy - 0.2, p2.l, p2.cy, { head: true, tail: true });
  seg(s, kb.r, kb.cy + 0.2, p3.l, p3.cy, { head: true, tail: true });
}
// ---------------------------------------------------------------- 도형 흐름도 슬라이드: 위에 도형, 아래 두세 줄
function drawn(title, draw, lines, notes, o = {}) {
  const s = content(title, "흐름", notes);
  draw(s);
  const lh = o.linesH || 1.0;
  bullets(s, lines, 0.5, 5.15 - lh, 9, lh, o.size || 11.5);
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
  drawOverview(s);
  legend(s, 4.55);
  para(s, "LLM 이 개입하는 곳은 두 곳(⑦, ⑨)뿐이고, 고쳐졌는지는 언제나 다음 빌드 결과로 판단합니다.", 0.5, 4.88, 9, 0.3, { size: 11, italic: true }); }

{ const s = content("접수와 분석", "흐름", "원본은 건드리지 않습니다. 작업용 사본에서 모든 단계를 git 으로 기록하므로 언제든 되돌릴 수 있습니다.");
  card(s, 0.5, 1.15, 4.4, 3.9, "① 접수", [
    "프로젝트 폴더나 zip 을 받으면 먼저 빌드 방식을 봅니다. Maven 은 그대로, Ant(build.xml)는 pom.xml 을 만들어 받습니다. Gradle 이거나 빌드 파일 없이 소스만 있는 폴더는 받지 않고 이유를 적어 돌려줍니다.",
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

drawn("검증 빌드와 수정 루프", drawLoop, [
  "⑧ mvn clean package 가 성공하면 바로 마무리합니다. 실패하면 ⑨ 수정 루프에 들어가 빌드가 될 때까지 돕니다.",
  "한 바퀴는 고치기 → 다시 빌드 → 채점입니다. 고친 뒤 빌드가 더 나빠지면 그 수정은 되돌립니다. 최대 50바퀴.",
  "두 수정이 같은 파일을 동시에 건드리지 않도록, 방금 손댄 파일의 오류는 다음 빌드 뒤에 봅니다."],
  "라운드 수와 LLM 호출 수는 설정으로 조절합니다. 한 바퀴에 다루는 케이스는 최대 8개입니다.", { linesH: 1.25, size: 11.5 });

drawn("오류 하나가 고쳐지는 길", drawLadder, [
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

drawn("한 번 고친 방법은 다음 프로젝트에서 다시 씁니다", drawKB, [
  "같은 모양의 오류는 어느 프로젝트에서 나와도 같은 케이스로 모입니다. 케이스마다 수정 스크립트와 성공·실패 횟수, 실패한 이유, 고치기 전·후 코드가 쌓입니다.",
  "프로젝트를 순서대로 돌리면 뒤로 갈수록 LLM 을 덜 부릅니다. 끝나면 보고서 맨 앞에 사람이 확인하거나 이어서 할 일이 적혀 있습니다.",
  "사람이 개입할 수 있습니다: 라이브러리 버전 표를 적어 주기, 잘못 배운 케이스 지우기, 고치지 말라고 표시하기."],
  "지식베이스는 파일 폴더입니다. 데이터베이스가 없으므로 복사해서 다른 PC 로 옮길 수 있습니다.", { linesH: 1.45, size: 11 });

(async () => { await pres.writeFile({ fileName: OUT }); if (applyTheme) await applyTheme(OUT, THEME); console.log("written", OUT); })().catch(e => { console.error(e); process.exit(1); });
