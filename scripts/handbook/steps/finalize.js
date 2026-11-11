// 마지막 빌드 뒤: 성공 판정, 오류 목록, 지식베이스 기록, 보고서, result.zip, 결과 한 줄, 종료 코드 0 / 2 / 3.
module.exports = {
  title: "⑪ 마무리: 결과를 판정하고 보고서와 지식을 남긴다",
  notes: "Orchestrator._finalize 가 차례로 한다: 성공 = 계획된 최종 빌드가 모두 돌고 모두 통과, restructure-failed · hollow-reactor · aborted 플래그 없음. " +
    "그다음 차단 항목 정리, 열린 오류를 unresolved 로 바꿔 실행 폴더에 errors.jsonl / errors.md, KB 에 kb.record_run(케이스별 seen · resolved · projects 갱신, cases.md 다시 씀), " +
    "report.md / report.json(인수인계 → Error list → Fixes by case → Structure review …), 마지막으로 changes.patch 와 result.zip(느린 것을 뒤에 두어 도중에 죽어도 보고서는 남는다). " +
    "스크립트 fix_v<N>.py 와 성공·실패 횟수(case.json)는 수정 루프가 시도마다 이미 기록했다. 종료 코드는 report_exit_code: 중단 플래그면 3, 성공이고 차단 없음이면 0, 그 밖은 2. " +
    "멈춤(stop.request)이면 최종 빌드 없이 여기로 오고, 내부 오류로 중단된 실행은 보고서 전에 최선을 다한 최종 빌드를 한 번 더 시도한다.",
  lines: [
    "성공은 모든 최종 빌드가 통과하고 구조 정리가 성공했으며 중단이 없을 때입니다",
    "담당팀은 보고서 맨 앞의 인수인계만 읽어도 할 일을 알 수 있습니다",
    "고친 방법은 지식베이스에 남아 다음 프로젝트가 LLM 없이 다시 씁니다",
  ],
  draw(s, H) {
    const w = 1.5, gx = 0.375, col = i => H.AREA.x + i * (w + gx);
    const r1 = 1.42, r2 = 2.45, r3 = 3.47, h = 0.7;
    const A = H.node(s, col(0), r1, w, h, "최종 빌드 결과", "⑩ 의 mvn clean package\n결과를 받습니다", "build");
    const B = H.node(s, col(1), r1, w, h, "성공 판정", "모든 빌드 통과,\n구조 정리 성공, 중단 없음", "auto");
    const C = H.node(s, col(2), r1, w, h, "오류 목록 정리", "못 고친 오류는 미해결로\n목록 파일에 적습니다", "auto");
    const D = H.node(s, col(3), r1, w, h, "지식베이스 기록", "케이스마다 본 횟수,\n해결 수, 프로젝트 갱신", "auto");
    const E = H.node(s, col(4), r1, w, h, "보고서 쓰기", "인수인계를 맨 앞에,\n구조 검토와 수정 내역", "auto");
    const F = H.node(s, col(4), r2, w, h, "결과 묶기", "바뀐 프로젝트를\nresult.zip 으로", "auto");
    const G = H.node(s, col(3), r2, w, h, "결과 한 줄", "SBMIG-RESULT 줄에\n빌드·오류·종료 코드", "auto");
    const Q = H.node(s, col(2), r2, w, h, "종료 코드는?", "보고서 내용으로 정합니다", "ask");
    const O3 = H.node(s, col(1), r3, w, 0.66, "3 중단", "멈춤·거부·준비 실패.\n이유는 보고서에", "loop");
    const O2 = H.node(s, col(2), r3, w, 0.66, "2 확인 필요", "빌드 실패나 차단이 남음.\n인수인계부터 봅니다", "loop");
    const O0 = H.node(s, col(3), r3, w, 0.66, "0 성공", "빌드 통과, 차단 없음", "done");
    const DL = H.node(s, col(4), r3, w, 0.66, "화면에서 받기", "report.md 와\nresult.zip 두 버튼", "auto");
    H.para(s, "스크립트와 그 성공·실패 기록은 수정 루프에서 시도마다 이미 쌓였습니다. 여기서는 이번 실행의 오류 목록을 지식베이스에 더합니다.",
      col(0), r2 + 0.05, 2 * w + gx - 0.1, 0.62, { size: 9, italic: true, color: "4A4F4A" });
    const hz = (p, q) => H.seg(s, p.r, p.cy, q.l, q.cy, { head: true });
    const back = (p, q) => H.seg(s, p.l, p.cy, q.r, q.cy, { head: true });
    hz(A, B); hz(B, C); hz(C, D); hz(D, E);
    H.seg(s, E.cx, E.b, F.cx, F.t, { head: true });
    back(F, G); back(G, Q);
    H.seg(s, F.cx, F.b, DL.cx, DL.t, { head: true });
    // 세 갈래: 왼쪽 3, 가운데 2, 오른쪽 0
    const my = (Q.b + O2.t) / 2;
    H.route(s, [[Q.cx - 0.45, Q.b], [Q.cx - 0.45, my], [O3.cx, my], [O3.cx, O3.t]], { head: true });
    H.label(s, (Q.cx - 0.45 + O3.cx) / 2, my, "중단", { w: 0.38 });
    H.seg(s, Q.cx, Q.b, O2.cx, O2.t, { head: true }); H.label(s, Q.cx, my, "그 밖", { w: 0.42 });
    H.route(s, [[Q.cx + 0.45, Q.b], [Q.cx + 0.45, my], [O0.cx, my], [O0.cx, O0.t]], { head: true });
    H.label(s, (Q.cx + 0.45 + O0.cx) / 2, my, "성공, 차단 없음", { w: 1.0 });
  },
};
