// ⑦ 구조 검토: LLM 이 재구성된 pom 을 회사 규칙에 비춰 보고 critical 만 편집으로 고치는 흐름. 어느 길이든 빌드로 간다.
module.exports = {
  title: "⑦ 구조 검토: 빌드 전에 LLM 이 pom 을 본다",
  notes: "구현은 sbmig/review/reviewer.py 의 StructureReviewer, 오케스트레이터 쪽은 _step_review, 프롬프트는 sbmig/review/prompts/review_v1.md 입니다. " +
    "LLM 에게 주는 재료(sbmig/review/inputs.py): 모든 pom, 모듈 구조, 회사 규칙(artifacts / layout 설정), 재구성이 한 일, 부모 체인에서 읽은 사실, 모듈별 소스 인코딩, 필터링 리소스 안의 바이너리 — 보낸 프롬프트는 logs/review-input.md(review.save_input). " +
    "LLM 호출은 최대 3번: 검토 1번, 답(또는 critical 옆의 편집)을 못 읽으면 1번, 편집 적용이 실패·pom 파싱 실패·불변 규칙 위반(review.invariant_check: 부모 체인 속성 재정의, 관리 버전 재고정, 낯선 parent, 필수 삭제, 떠도는 pom 복사본을 모듈로 넣어 reactor 중복)으로 되돌렸으면 이유를 주고 1번 — 두 번째도 안 되면 되돌린 채 인수인계합니다. " +
    "소스(.java / .jsp)를 고치는 편집은 거부하고, 재료에 없는 버전·속성을 말한 지적은 적용하지 않습니다. UTF-8 이 아닌 소스에 인코딩 설정이 없으면 LLM 이 놓쳐도 sbmig 가 critical 로 더합니다. " +
    "적용된 편집은 'review: fix critical structure issues (n)' 커밋. review.enabled: false 나 드라이런이면 skipped, LLM 없음·전송 오류·두 번 못 읽은 답은 failed 로 적고 어느 경우든 검증 빌드로 갑니다(review.fix_critical: false 면 보고만).",
  lines: [
    "LLM 호출은 많아야 3번: 검토, 못 읽은 답 다시 받기, 적용 실패 뒤 다시 받기",
    "warning 은 고치지 않고 보고서의 구조 검토 절과 오류 목록에만 남깁니다",
    "LLM 이 없거나 답을 끝내 못 읽어도 실패로 적고 빌드는 그대로 진행합니다",
  ],
  draw(s, H) {
    const w = 1.5, gap = 0.375, h = 0.68, ys = [1.67, 2.62];
    const x = c => 0.5 + c * (w + gap);
    const at = (c, r, title, desc, kind) => H.node(s, x(c), ys[r], w, h, title, desc, kind);
    const on = at(0, 0, "검토를 하나?", "켜져 있고 LLM 을 쓸 수 있나", "ask");
    const inp = at(1, 0, "재료 모으기", "pom·모듈·회사 규칙·재구성 기록·인코딩", "auto");
    const llm = at(2, 0, "LLM 검토", "판정, critical / warning 지적, 고칠 편집", "llm");
    const read = at(3, 0, "답을 읽었나?", "약속한 형식인지 확인합니다", "ask");
    const crit = at(4, 0, "critical 있나?", "빌드를 막거나 규칙을 어긴 것", "ask");
    const apply = at(3, 1, "편집 적용", "pom 등 빌드 파일만, 소스 편집은 거부", "auto");
    const chk = at(2, 1, "제대로 적용됐나?", "pom 이 깨지거나 회사 규칙을 어겼나", "ask");
    const commit = at(1, 1, "검토 커밋", "git 에 review: … 로 남깁니다", "done");
    const build = H.node(s, 0.5, 3.57, 9, 0.56, "⑧ 검증 빌드 (mvn clean package)",
      "검토가 어떻게 끝나든 빌드로 갑니다. 성공 여부는 빌드가 정합니다", "build");
    const hz = (p, q) => H.seg(s, p.r, p.cy, q.l, q.cy, { head: true });
    hz(on, inp); H.label(s, (on.r + inp.l) / 2, on.cy - 0.2, "예", { w: 0.3 });
    H.seg(s, on.cx, on.b, on.cx, build.t, { head: true }); H.label(s, on.cx, (on.b + ys[1]) / 2, "아니오", { w: 0.45 });
    hz(inp, llm); hz(llm, read);
    hz(read, crit); H.label(s, (read.r + crit.l) / 2, read.cy - 0.2, "예", { w: 0.3 });
    // 못 읽은 답: 위로 돌아 LLM 에 한 번 더
    const top = 1.53;
    H.route(s, [[read.cx, read.t], [read.cx, top], [llm.cx, top], [llm.cx, llm.t]]);
    H.label(s, (read.cx + llm.cx) / 2, top, "못 읽음: 한 번 더", { w: 1.1 });
    // critical: 예 → 편집 적용, 아니오 → 바로 빌드
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
