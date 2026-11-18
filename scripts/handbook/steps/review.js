// ⑦ 구조 검토: LLM 이 재구성된 pom 을 회사 규칙에 비춰 보고 심각한 문제만 pom 편집으로 고치는 흐름. 어느 길이든 빌드로 간다.
module.exports = {
  title: "⑦ 구조 검토: 빌드 전에 LLM이 pom.xml을 점검합니다",
  notes: "구조 정리가 끝나면 첫 빌드 전에 LLM이 바뀐 pom.xml을 한 번 훑어봅니다. 규칙으로 바꾼 결과가 회사 규칙에 맞는지, 빌드를 막을 문제가 없는지를 사람 대신 미리 보는 단계입니다. " +
    "LLM에게는 모든 pom.xml, 모듈 구조, 회사 규칙(사내 parent, 필수·선택 라이브러리, 모듈 배치), 구조 정리가 한 일, 사내 부모 pom이 정해 주는 버전, 모듈별 소스 인코딩, 리소스 필터링 폴더 안의 바이너리 파일 목록을 함께 보냅니다.\n\n" +
    "LLM은 문제마다 심각한 문제인지 주의 사항인지를 나눠 답합니다. 심각한 문제는 LLM이 보낸 pom.xml 수정을 그 자리에서 적용하고, 따로 git 커밋으로 남겨 나중에 되돌릴 수 있게 합니다. " +
    "고칠 수 있는 것은 pom.xml 같은 빌드 파일뿐이며, Java나 JSP 소스를 고치려는 수정은 받지 않습니다. 보낸 자료에 없는 버전이나 설정을 근거로 든 지적도 적용하지 않습니다. " +
    "적용한 뒤에는 pom.xml이 제대로 읽히는지, 구조 정리가 정한 것(부모 pom이 정한 버전과 속성, 사내 parent, 필수 라이브러리)을 거꾸로 되돌리지 않았는지 다시 확인합니다. " +
    "어긋나면 원래대로 되돌리고 이유를 알려 준 뒤 한 번 더 받습니다. 두 번째도 맞지 않으면 되돌린 상태로 두고 보고서에 남깁니다.\n\n" +
    "LLM은 많아야 세 번 부릅니다. 검토 한 번, 답을 읽을 수 없을 때 한 번, 수정이 맞지 않아 되돌린 뒤 한 번입니다. " +
    "UTF-8이 아닌 소스(MS949 등)에 인코딩 설정이 빠진 모듈은 LLM이 놓쳐도 sbmig가 심각한 문제로 따로 표시합니다.\n\n" +
    "주의 사항은 고치지 않습니다. 고치지 못한 심각한 문제와 함께 보고서의 「확인해 주세요」에 적어 드리고, 심각한 문제와 주의 사항이 몇 건이었는지는 보고서 「참고」의 실행 정보에서 볼 수 있습니다." +
    "회사 설정에서는 LLM이 문제 설명을 한국어로 쓰도록 요청합니다.\n\n" +
    "검토를 끈 설정이거나 LLM을 쓸 수 없거나 답을 끝내 읽지 못해도 실행은 멈추지 않고 빌드로 넘어갑니다. 성공 여부는 언제나 빌드가 정합니다. " +
    "사내 프레임워크를 쓰는 프로젝트는 구조 정리를 하지 않으므로 이 검토도 하지 않습니다.",
  lines: [
    "LLM은 많아야 세 번 부릅니다. 검토 한 번, 답을 다시 받는 데 한 번, 수정을 고쳐 받는 데 한 번입니다.",
    "심각한 문제는 pom.xml을 고쳐 바로 반영하고, 주의 사항은 보고서의 「확인해 주세요」에 적습니다.",
    "검토를 하지 못해도 실행은 멈추지 않고 빌드로 넘어갑니다. 사내 프레임워크 프로젝트는 검토하지 않습니다.",
  ],
  draw(s, H) {
    const w = 1.5, gap = 0.375, h = 0.68, ys = [1.67, 2.62];
    const x = c => 0.5 + c * (w + gap);
    const at = (c, r, title, desc, kind) => H.node(s, x(c), ys[r], w, h, title, desc, kind);
    const on = at(0, 0, "검토할 수 있나?", "켜져 있고 LLM을 쓸 수 있나", "ask");
    const inp = at(1, 0, "자료 모으기", "pom.xml·모듈·회사 규칙·인코딩", "auto");
    const llm = at(2, 0, "LLM 검토", "문제를 찾고 pom.xml 수정을 제안", "llm");
    const read = at(3, 0, "답을 읽었나?", "정해진 형식으로 왔는지", "ask");
    const crit = at(4, 0, "심각한 문제?", "빌드를 막거나 규칙을 어긴 것", "ask");
    const apply = at(3, 1, "pom.xml 고치기", "빌드 파일만, 소스는 건드리지 않음", "auto");
    const chk = at(2, 1, "제대로 고쳤나?", "pom.xml이 깨지거나 규칙을 어겼나", "ask");
    const commit = at(1, 1, "수정 저장", "git에 따로 커밋합니다", "done");
    const build = H.node(s, 0.5, 3.57, 9, 0.56, "⑧ 검증 빌드",
      "검토가 어떻게 끝나든 빌드로 갑니다. 주의 사항은 보고서의 「확인해 주세요」에 남습니다", "build");
    const hz = (p, q) => H.seg(s, p.r, p.cy, q.l, q.cy, { head: true });
    hz(on, inp); H.label(s, (on.r + inp.l) / 2, on.cy - 0.2, "예", { w: 0.3 });
    H.seg(s, on.cx, on.b, on.cx, build.t, { head: true }); H.label(s, on.cx, (on.b + ys[1]) / 2, "아니오", { w: 0.45 });
    hz(inp, llm); hz(llm, read);
    hz(read, crit); H.label(s, (read.r + crit.l) / 2, read.cy - 0.2, "예", { w: 0.3 });
    // 못 읽은 답: 위로 돌아 LLM 에 한 번 더
    const top = 1.53;
    H.route(s, [[read.cx, read.t], [read.cx, top], [llm.cx, top], [llm.cx, llm.t]]);
    H.label(s, (read.cx + llm.cx) / 2, top, "못 읽음: 한 번 더", { w: 1.1 });
    // 심각한 문제: 예 → 고치기, 아니오 → 바로 빌드
    const xy = crit.l + 0.25;
    H.route(s, [[xy, crit.b], [xy, apply.cy], [apply.r, apply.cy]]);
    H.label(s, xy, (crit.b + ys[1]) / 2, "예", { w: 0.3 });
    const xn = crit.cx + 0.3;
    H.seg(s, xn, crit.b, xn, build.t, { head: true }); H.label(s, xn, (crit.b + ys[1]) / 2, "아니오", { w: 0.45 });
    hz2(apply, chk); hz2(chk, commit);
    H.label(s, (chk.l + commit.r) / 2, chk.cy - 0.2, "예", { w: 0.3 });
    // 규칙 위반·적용 실패: 되돌리고 이유를 주며 한 번 더
    H.seg(s, chk.cx, chk.t, llm.cx, llm.b, { head: true });
    H.label(s, chk.cx + 0.83, (llm.b + chk.t) / 2, "아니오: 되돌리고 한 번 더", { w: 1.55 });
    H.seg(s, commit.cx, commit.b, commit.cx, build.t, { head: true });
    function hz2(p, q) { H.seg(s, p.l, p.cy, q.r, q.cy, { head: true }); }
  },
};
