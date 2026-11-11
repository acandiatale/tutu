// ⑧ ~ ⑩ 검증 빌드, 수정 루프, 최종 빌드를 한 장에: 첫 빌드 → 실패면 케이스마다 한 가지 시도 → 다시 빌드, 멈추는 곳 세 군데.
module.exports = {
  title: "⑧ ~ ⑩ 빌드와 수정: 통과할 때까지 고치고 빌드",
  notes: "빌드는 매번 mvn clean package -Dmaven.test.skip=true 입니다(build.skip_test_compile: true, 테스트는 범위 밖). " +
    "첫 빌드가 실패하면 그 빌드가 수정 루프의 첫 빌드가 되고(sbmig/loop/orchestrator.py _step_verify → sbmig/loop/fixloop.py BuildFixLoop), " +
    "한 바퀴에 케이스는 기본 8개까지(loop.max_clusters_per_iteration), 이번 바퀴에 이미 손댄 파일이나 모듈 pom 의 케이스는 다음 빌드까지 미룹니다. " +
    "케이스마다 순서: LLM 없는 사전 시도(sbmig-version → sbmig-resources → sbmig-bundled → sbmig-jakarta, 맞는 것 하나) → 저장된 스크립트 1개 재사용(script.reuse_versions) → " +
    "LLM 보강(script.max_augments_per_case 15) → 새로 작성, LLM 요청이 script.max_llm_tries_per_case(20)에 닿으면 repeated_failure 로 막힙니다(직접 수정만 하는 케이스는 4번, 거절은 1번). " +
    "루프는 언제나 빌드 뒤에서 끝납니다: 성공, 바퀴 한도(loop.phases.verify.max_iterations 기본 50, 같은 빌드로 다시 묻는 바퀴도 셈), 막힌 케이스만 남음, 더 할 시도 없음, 시간 한도(loop.max_total_minutes 240). " +
    "루프를 돌았으면 최종 빌드를 한 번 더 하고 그 결과가 종료 코드를 정합니다(0 = 성공이고 막힌 것 없음, 2 = 오류나 막힌 케이스가 남음, 3 = 사내 parent 를 못 찾는 등 중단).",
  lines: [
    "빌드는 매번 mvn clean package, 테스트는 건너뜁니다. 첫 빌드가 성공하면 바로 끝납니다.",
    "실패하면 케이스마다 한 가지씩 시도하고 다시 빌드해 채점합니다. 도움 안 된 수정은 되돌립니다.",
    "바퀴 한도에 닿거나 막힌 케이스만 남으면 멈추고, 막힌 이유를 보고서 맨 앞에 적어 넘깁니다.",
  ],
  draw(s, H) {
    const w = 1.5, h = 0.66, g = 0.3, x0 = H.AREA.x;
    const col = i => x0 + i * (w + g);
    const ys = [1.66, 2.56, 3.46];
    const A = H.node(s, col(0), ys[0], w, h, "⑧ 검증 빌드", "mvn clean package, 바퀴마다 다시 돕니다", "build");
    const B = H.node(s, col(0), ys[1], w, h, "빌드 성공?", "실패면 로그에서 오류를 읽습니다", "ask");
    const E = H.node(s, col(0), ys[2], w, h, "⑩ 최종 빌드", "루프를 돌았으면 한 번 더, 이것이 결과", "build");
    const C = H.node(s, col(1), ys[1], w, h, "케이스로 묶기", "같은 원인은 한 케이스, 근본 원인부터", "auto");
    const Q = H.node(s, col(2), ys[1], w, h, "더 시도할까?", "바퀴 한도 전이고 안 막힌 케이스가 있나", "ask");
    const K = H.node(s, col(2), ys[2], w, h, "사람에게 인계", "막힌 케이스를 이유와 함께 보고서에", "loop");
    const P = H.node(s, col(3), ys[0], w, h, "LLM 없는 시도", "빠진 버전, jakarta 빌드처럼 정해진 처리", "auto");
    const G = H.node(s, col(3), ys[1], w, h, "저장된 스크립트", "있으면 LLM 없이 그대로 실행", "auto");
    const L = H.node(s, col(3), ys[2], w, h, "LLM 수정", "스크립트를 보강하거나 새로 받습니다", "llm");
    const R = H.node(s, col(4), ys[1], w, h, "커밋", "다음 빌드가 채점, 안 되면 되돌림", "loop");

    // 첫 빌드 → 성공? ; 성공 → 최종 빌드 ; 실패 → 묶기 → 더 시도할까?
    H.seg(s, A.cx, A.b, B.cx, B.t, { head: true });
    H.seg(s, B.cx, B.b, E.cx, E.t, { head: true }); H.label(s, B.cx + 0.3, (B.b + E.t) / 2, "성공", { w: 0.4 });
    H.seg(s, B.r, B.cy, C.l, C.cy, { head: true }); H.label(s, (B.r + C.l) / 2, B.cy - 0.2, "실패", { w: 0.3 });
    H.seg(s, C.r, C.cy, Q.l, Q.cy, { head: true });
    // 아니오 → 인계 → 최종 빌드
    H.seg(s, Q.cx, Q.b, K.cx, K.t, { head: true }); H.label(s, Q.cx + 0.35, (Q.b + K.t) / 2, "아니오", { w: 0.45 });
    H.seg(s, K.l, K.cy, E.r, E.cy, { head: true });
    // 예 → 사다리: LLM 없는 시도 → 저장된 스크립트 → LLM
    H.route(s, [[Q.cx, Q.t], [Q.cx, P.cy], [P.l, P.cy]]); H.label(s, Q.cx + 0.18, (Q.t + P.cy) / 2 + 0.05, "예", { w: 0.25 });
    H.seg(s, P.cx, P.b, G.cx, G.t, { head: true }); H.label(s, P.cx + 0.47, (P.b + G.t) / 2, "해당 없음", { w: 0.65 });
    H.seg(s, G.cx, G.b, L.cx, L.t, { head: true }); H.label(s, G.cx + 0.52, (G.b + L.t) / 2, "없거나 실패", { w: 0.75 });
    // 셋 다 커밋 → 다시 빌드로
    H.route(s, [[P.r, P.cy], [R.cx, P.cy], [R.cx, R.t]]);
    H.seg(s, G.r, G.cy, R.l, R.cy, { head: true });
    H.route(s, [[L.r, L.cy], [R.cx, L.cy], [R.cx, R.b]]);
    // 다음 바퀴: 오른쪽으로 돌아 맨 위를 지나 빌드로
    const xr = R.r + 0.15, top = 1.52;
    H.route(s, [[R.r, R.cy], [xr, R.cy], [xr, top], [A.cx, top], [A.cx, A.t]]);
    H.label(s, (A.cx + xr) / 2, top, "다음 바퀴 (기본 50바퀴까지)", { w: 1.8 });
  },
};
