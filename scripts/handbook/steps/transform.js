// ⑤ 자동 변환: 계획한 레시피를 OpenRewrite 한 번에 돌리고, 실패나 풀리지 않은 버전을 어떻게 다루는지.
module.exports = {
  title: "⑤ 자동 변환: 레시피를 한 번에 돌리고 커밋한다",
  notes: "실행기는 sbmig/rewrite/runner.py 의 OpenRewriteRunner, 단계 처리는 sbmig/loop/orchestrator.py 의 _step_rewrite 입니다. " +
    "계획한 레시피를 합성 레시피 하나(rewrite.yml)로 묶어 rewrite-maven-plugin 6.46.1 의 run 으로 돌리고(레시피 BOM 3.37.0), JSP·HTML 은 openrewrite.exclusions 로 빼며 한 번의 시간 상한은 openrewrite.timeout_minutes(기본 40분)입니다. " +
    "다시 시도: OutOfMemoryError 면 oom_retry_jvm_args(기본 -Xmx8g)로 한 번, 다운로드 실패면 download_retry_wait_s(30초) 뒤 download_retry(기본 1)번, 묵은 lastUpdated 표식이면 -U 로 한 번, fork 된 compile 에서 죽으면 runNoFork 로 한 번. " +
    "그래도 실패하면 오류 목록에 rewrite 오류로 적고 계속 갑니다. 예외: 레시피 id 를 못 찾으면 설정 오류로 변환 묶음을 멈추고, 사내 부모 pom 을 못 받아 실패했으면 수정 루프와 같이 실행을 멈춥니다(parent_not_found). " +
    "성공했는데 pom 에 4.0.x / latest.release 같은 글자가 남으면 되돌리고 기다렸다 한 번 더, 또 남으면 플래그 rewrite-unresolved-versions + 인수인계. 파싱 못 한 파일은 rewrite-parse-problems. 커밋 메시지는 'rewrite: transform.rewrite'. plan.compile_after_rewrite: true 일 때만 뒤에 컴파일이 한 번 붙습니다.",
  lines: [
    "레시피는 한 번에 한 번만 돕니다. 앞뒤로 빌드하지 않고, 결과는 구조 정리 뒤의 빌드가 봅니다",
    "라이브러리별 레시피는 넣지 않습니다. 무엇을 바꿀지 모르는 규칙 대신 빌드 오류로 고칩니다",
    "JSP 는 OpenRewrite 가 읽지 않습니다. JSP 안의 javax 이름은 구조 정리가 바꿉니다",
  ],
  draw(s, H) {
    const A = H.AREA;
    const y1 = 1.72, h1 = 0.76, y2 = 3.3, h2 = 0.82;
    const a = H.node(s, 0.5, y1, 1.5, h1, "레시피 묶기", "계획한 레시피를 한 묶음으로", "auto");
    const b = H.node(s, 2.25, y1, 1.65, h1, "OpenRewrite 실행", "Maven 플러그인으로, 지금 의존성 그대로", "build");
    const c = H.node(s, 4.2, y1, 1.4, h1, "끝까지 돌았나?", "성공으로 끝났는지", "ask");
    const f = H.node(s, 6.1, y1, 1.5, h1, "풀리지 않은 버전?", "pom 에 4.0.x 같은 글자가 남았나", "ask");
    const h = H.node(s, 8.1, y1, 1.4, h1, "git 커밋", "바뀐 파일이 있을 때만", "auto");
    const d = H.node(s, 2.25, y2, 1.65, h2, "방식 바꿔 재시도", "메모리 부족·다운로드·컴파일 실패면 한 번 더", "loop");
    const g = H.node(s, 6.1, y2, 1.5, h2, "되돌리고 다시", "기다렸다 한 번 더, 또 남으면 보고서에", "loop");
    const done = H.node(s, 8.1, y2, 1.4, h2, "구조 정리로", "중간 빌드 없이 넘어갑니다", "done");
    const hz = (p, q) => H.seg(s, p.r, p.cy, q.l, q.cy, { head: true });
    hz(a, b); hz(b, c);
    hz(c, f); H.label(s, (c.r + f.l) / 2, c.cy - 0.17, "성공", { w: 0.36 });
    hz(f, h); H.label(s, (f.r + h.l) / 2, f.cy - 0.17, "아니오", { w: 0.45 });
    // 실패 → 재시도 → 다시 실행
    H.route(s, [[c.cx, c.b], [c.cx, d.cy], [d.r, d.cy]]);
    H.label(s, c.cx, (c.b + d.t) / 2, "실패", { w: 0.36 });
    H.seg(s, d.cx, d.t, b.cx, b.b, { head: true });
    H.label(s, d.cx + 0.4, (b.b + d.t) / 2, "한 번 더", { w: 0.55 });
    // 또 실패 → 오류로 적고 커밋 쪽으로(멈추지 않음)
    const top = A.t + 0.13;
    H.route(s, [[c.cx, c.t], [c.cx, top], [h.cx, top], [h.cx, h.t]]);
    H.label(s, (c.cx + h.cx) / 2, top, "그래도 실패: 오류 목록에 적고 계속", { w: 2.4 });
    // 풀리지 않은 버전 → 되돌리고 다시 → 커밋
    H.seg(s, f.cx, f.b, g.cx, g.t, { head: true });
    H.label(s, f.cx - 0.25, (f.b + g.t) / 2, "예", { w: 0.3 });
    const mid = (h.b + done.t) / 2;
    H.route(s, [[g.r - 0.25, g.t], [g.r - 0.25, mid], [h.l + 0.3, mid], [h.l + 0.3, h.b]]);
    H.seg(s, h.cx + 0.2, h.b, h.cx + 0.2, done.t, { head: true });
  },
};
