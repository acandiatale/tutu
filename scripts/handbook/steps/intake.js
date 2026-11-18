// ② 접수: 사본·git → 사전 점검(settings.xml, Maven, JDK 21) → 빌드 파일 확인(Maven 만, Ant·Gradle·빌드 파일 없음은 거부) → 취약점 해결만이면 그대로 넘김 → pom 정리 → 원래 버전 기록.
module.exports = {
  title: "② 접수: 받을 수 있는 프로젝트인지 보고 작업 사본을 만듭니다",
  notes: "접수에서는 올려 주신 프로젝트를 이 도구가 다룰 수 있는지 먼저 가립니다. 원본은 그대로 두고 사본을 만들어, 이 사본을 git 저장소로 만듭니다. 첫 커밋이 받은 그대로의 상태이고, 이후 단계에서 바꾸는 것은 모두 커밋으로 쌓이기 때문에 나중에 무엇이 바뀌었는지 하나하나 따라갈 수 있습니다. IDE 설정 파일이나 target 같은 빌드 산출물은 사본에 넣지 않습니다. " +
    "다음은 사전 점검입니다. 빌드 서버에 Maven 설정 파일(settings.xml)이 제대로 놓여 있는지, Maven 이 실행되는지, 그 Maven 이 JDK 21 로 도는지 봅니다. 셋 중 하나라도 어긋나면 프로젝트 문제가 아니라 서버 준비 문제이므로, 프로젝트를 손대기 전에 멈추고 화면과 보고서에 이유를 보여 드립니다. 이때는 운영 담당자에게 알려 주세요. " +
    "그다음 빌드 파일을 봅니다. 이 도구는 Maven 프로젝트만 받습니다. Ant 프로젝트는 회사 결정에 따라 자동으로 Maven 으로 바꾸지 않고, 'Maven 변환 후 신청 부탁드립니다.' 라는 안내와 함께 돌려보냅니다. Maven 으로 옮긴 뒤 다시 올려 주시면 됩니다. Gradle 프로젝트나 빌드 파일이 없는 폴더도 받지 않습니다. " +
    "취약점 해결만을 고르셨다면 여기까지만 확인하고, 프로젝트는 한 글자도 바꾸지 않은 채 바로 취약점 해결로 넘어갑니다. " +
    "전체 진행이면 pom 을 정리합니다. IDE 출력 폴더나 백업 폴더에 통째로 복사된 pom 은 모듈로 보지 않고 무시합니다. 루트 pom 이 없고 형제 pom 들만 있으면 이들을 묶는 루트 pom 을 만들어 줍니다. 같은 모듈이 두 번 묶여 있으면 하나만 남기고, 배포에만 쓰는 빌드 확장은 빌드를 막을 수 있어 뺍니다. 코드가 들어 있는 모듈 없이 EAR 만 올라온 경우는 받지 않습니다. 무시하거나 바꾼 것은 보고서의 「확인해 주세요」에 적습니다. " +
    "마지막으로 Maven 으로 지금 쓰고 있는 라이브러리 버전표를 한 번 뽑아 둡니다. 나중에 새 부모 pom 이 정해 주지 않는 버전을 이어받을 때와, 보고서의 버전 비교 표에 씁니다.",
  lines: [
    "원본은 건드리지 않습니다. 사본을 git 저장소로 만들고, 이후 바뀌는 것은 모두 커밋으로 남깁니다.",
    "Maven 프로젝트만 받습니다. Ant 프로젝트는 'Maven 변환 후 신청 부탁드립니다.' 안내와 함께 돌려보냅니다.",
    "취약점 해결만을 고르셨다면 점검만 하고, 프로젝트는 그대로 둔 채 바로 취약점 해결로 넘어갑니다.",
  ],
  draw(s, H) {
    const w = 1.55, g = (9 - 5 * w) / 4, h = 0.8, y1 = 1.5, y2 = 3.2, mid = 2.75;
    const col = i => H.AREA.x + i * (w + g);
    const o = { descSize: 9 };
    const a = H.node(s, col(0), y1, w, h, "사본 만들기", "원본은 두고 복사해 git 저장소로", "auto", o);
    const b = H.node(s, col(1), y1, w, h, "사전 점검", "Maven 설정 파일,\nMaven, JDK 21", "ask", o);
    const c = H.node(s, col(2), y1, w, h, "빌드 파일은?", "pom.xml 이 있어야 받습니다", "ask", o);
    const d = H.node(s, col(3), y1, w, h, "pom 정리", "전체 진행일 때. 복사본 pom 무시, 루트 pom 생성", "auto", o);
    const e = H.node(s, col(4), y1, w, h, "원래 버전 기록", "지금 쓰는 라이브러리\n버전표를 저장", "build", o);
    const r = H.node(s, col(0), y2, 2 * w + g, h, "여기서 멈춤", "화면과 보고서에 이유가 나옵니다. Ant 는 'Maven 변환 후 신청' 안내", "loop", o);
    const rem = H.node(s, col(2), y2, w, h, "취약점 해결로", "프로젝트는 바꾸지 않고 넘깁니다", "done", o);
    const f = H.node(s, col(4), y2, w, h, "③ 분석으로", "바꾼 것은 모두 커밋으로", "done", o);
    // 본선
    H.seg(s, a.r, a.cy, b.l, b.cy, { head: true });
    H.seg(s, b.r, b.cy, c.l, c.cy, { head: true });
    H.seg(s, c.r, c.cy, d.l, d.cy, { head: true });
    H.seg(s, d.r, d.cy, e.l, e.cy, { head: true });
    H.seg(s, e.cx, e.b, f.cx, f.t, { head: true });
    // 사전 점검 실패 → 멈춤
    H.seg(s, b.cx, b.b, b.cx, r.t, { head: true });
    H.label(s, b.cx, mid, "어긋남", { w: 0.5 });
    // Ant·Gradle·빌드 파일 없음 → 멈춤
    const gx = c.l + 0.25, rx = r.r - 0.25;
    H.route(s, [[gx, c.b], [gx, mid], [rx, mid], [rx, r.t]]);
    H.label(s, (gx + rx) / 2, mid, "pom.xml 없음", { w: 0.8 });
    // 취약점 해결만 → 그대로 넘김
    const ax = c.cx + 0.25;
    H.seg(s, ax, c.b, ax, rem.t, { head: true });
    H.label(s, ax, mid, "취약점 해결만", { w: 0.85 });
  },
};
