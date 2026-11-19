// ⑫ 마무리: 취약점 조치 뒤(그래프의 finalize·remediation 노드가 끝나면) Orchestrator._finalize 가 판정 → 오류 기록(errors.md) →
// 지식베이스 기록 → 보고서(report.md, 요청자 안내서, 취약점 조치 요약 포함) → changes.patch → result.zip 순서로 만든다.
// 확인한 코드: sbmig/loop/orchestrator.py (_run_guard, build_succeeded, _finalize, _finalize_errors), sbmig/report/guide_ko.py
// (head_lines, purpose_lines), sbmig/web/static/index.html (보고서 받기 / 변환된 프로젝트 받기).
module.exports = {
  title: "⑫ 마무리: 결과를 정리해 보고서와 파일로 드립니다",
  notes: "마지막 단계에서는 결과를 판정하고, 보고서와 결과 파일을 만들어 드립니다. 취약점 조치를 이어서 했다면 그것까지 끝난 뒤에 이 단계가 진행되고, 이관 보고서에 조치 결과가 짧게 들어갑니다. 그래서 보고서와 결과 파일은 취약점 조치까지 끝나야 내려받을 수 있습니다.\n\n" +
    "결과는 세 가지 중 하나입니다. 계획한 최종 빌드가 모두 통과하면 빌드 성공입니다. 서비스가 여러 개인 프로젝트는 모두 통과해야 하고, 구조 정리가 실패했거나 소스가 실제로 빌드 대상에 들어가지 않았다면 빌드가 통과해도 성공으로 보지 않습니다. 사내 프레임워크 프로젝트는 변환이 전부이므로, 자동 변환(OpenRewrite)이 실패했으면 빌드가 통과해도 성공이 아니고 맨 위에 '변환 실패 (빌드는 통과)'로 나옵니다. 다른 프로젝트도 자동 변환이 실패하면 그 사실과 원인이 「확인해 주세요」의 첫 항목에 나옵니다. 빌드가 남은 오류로 끝나면 빌드 실패, 처음 단계에서 받지 않았거나 멈춤을 눌렀거나 환경 문제로 끝까지 가지 못했으면 실행 중단입니다. 중단된 경우에도 최종 빌드를 해 볼 수 있으면 한 번 해 보고, 남은 오류를 보고서에 적습니다.\n\n" +
    "그다음 이번 실행에서 나온 모든 오류와 처리 과정을 errors.md에 정리합니다. 끝까지 남은 오류는 해결하지 못한 것으로 닫습니다. 같은 내용을 지식베이스에도 더해서, 다음 프로젝트가 같은 오류를 만나면 이번에 고친 방법을 바로 쓰게 합니다.\n\n" +
    "보고서 report.md는 요청하신 분께 드리는 안내서입니다. 읽는 법은 뒤에서 따로 설명드립니다. 보고서를 먼저 쓰고, 시간이 걸리는 changes.patch와 result.zip은 맨 마지막에 만듭니다. 결과를 묶다가 문제가 생겨도 보고서는 남게 하려는 순서입니다.\n\n" +
    "화면에서는 '보고서 받기'로 report.md를, '변환된 프로젝트 받기'로 result.zip을 받습니다. result.zip은 이관만 한 프로젝트입니다. 취약점 조치까지 반영된 프로젝트는 그 아래 취약점 조치 영역의 결과 ZIP으로 받으셔야 합니다.\n\n" +
    "보고서 맨 위 표에는 SBMIG-RESULT로 시작하는 결과 한 줄이 있습니다. 빌드 결과, 남은 오류 수, 취약점 조치 결과가 이 한 줄에 들어 있으니, 문의하실 때 이 줄을 함께 보내 주시면 됩니다.",
  lines: [
    "결과는 빌드 성공, 빌드 실패, 실행 중단 가운데 하나로(사내 프레임워크는 변환 실패도) 보고서 맨 위에 나옵니다. 취약점 조치를 했다면 그 결과도 보고서에 들어갑니다.",
    "모든 오류와 처리 과정은 errors.md에, 고친 방법은 지식베이스에 남겨 다음 프로젝트가 다시 씁니다.",
    "화면에서 보고서(report.md)와 변환된 프로젝트(result.zip)를 받습니다. 취약점 조치가 반영된 프로젝트는 취약점 조치 영역에서 받습니다.",
  ],
  draw(s, H) {
    const w = 1.95, gx = 0.4, h = 0.68, col = i => H.AREA.x + i * (w + gx);
    const r1 = 1.45, r2 = 2.43, r3 = 3.42;
    const A = H.node(s, col(0), r1, w, h, "최종 빌드와 취약점 조치", "둘 다 끝난 뒤에 시작합니다", "build");
    const B = H.node(s, col(1), r1, w, h, "결과 판정", "빌드 성공 · 빌드 실패 · 실행 중단", "ask");
    const C = H.node(s, col(2), r1, w, h, "오류 기록 정리", "모든 오류와 처리 과정을 errors.md에", "auto");
    const D = H.node(s, col(3), r1, w, h, "지식베이스에 저장", "고친 방법을 다음 프로젝트가 다시 씁니다", "auto");
    const E = H.node(s, col(3), r2, w, h, "보고서 쓰기", "요청하신 분께 드리는 안내서 report.md", "done");
    const F = H.node(s, col(2), r2, w, h, "결과 파일 묶기", "변환된 프로젝트 result.zip, 전체 변경 changes.patch", "auto");
    const G = H.node(s, col(1), r2, w, h, "화면에서 받기", "보고서 받기, 변환된 프로젝트 받기", "auto");
    const hz = (p, q) => H.seg(s, p.r, p.cy, q.l, q.cy, { head: true });
    const back = (p, q) => H.seg(s, p.l, p.cy, q.r, q.cy, { head: true });
    hz(A, B); hz(B, C); hz(C, D);
    H.seg(s, D.cx, D.b, E.cx, E.t, { head: true });
    back(E, F); back(F, G);
    H.para(s, "보고서를 열면 맨 위 결과에 따라 먼저 볼 곳이 다릅니다", H.AREA.x, r3 - 0.27, 9, 0.24, { size: 9.5, italic: true, color: "4A4F4A" });
    H.node(s, col(0), r3, w, h, "빌드 성공", "「확인해 주세요」부터 봅니다", "done");
    H.node(s, col(1), r3, w, h, "빌드 실패", "「해결하지 못한 오류」와 「진행하지 않은 항목」부터", "loop");
    H.node(s, col(2), r3, w, h, "실행 중단", "보고서 첫머리에 멈춘 이유가 있습니다", "loop");
    H.node(s, col(3), r3, w, h, "문의할 때", "보고서 맨 위의 결과 한 줄을 함께 보내 주세요", "ask");
  },
};
