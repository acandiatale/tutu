// ② 접수: 사본·git → 사전 점검 → 빌드 파일 판별(Ant 면 pom 합성, 그 밖은 거부) → pom 정리 → as-is 버전 기록.
module.exports = {
  title: "② 접수: 받을 프로젝트를 가리고 작업 사본을 만든다",
  notes: "입력 폴더나 zip 은 out/<run id>/project/ 로 복사되고(IDE·VCS 잡동사니와 빌드 산출물 target/ 은 빠짐) 브랜치 sbmig/<run id> 의 첫 커밋 'sbmig: as-is snapshot' 이 됩니다. " +
    "사전 점검은 mvn -v 가 도는지(mvn-not-found)와 그 Java 가 build.require_java_major(21)인지(jdk-not-21)를 보고, 어긋나면 종료 코드 3 으로 끝냅니다. 사내 부모(artifacts.parent) 좌표가 비어 있으면 멈추지 않고 후속 조치 한 줄만 남기며, 부모 체인의 빌드 확장을 못 읽으면 플래그 parent-extension-unresolvable 로 경고만 합니다. " +
    "pom.xml 이 어디든 하나 있으면 Maven 으로 보고, build.xml 만 있으면 intake.ant_to_maven 일 때 sbmig/analyze/ant.py 가 pom.xml 을 합성해 커밋합니다(좌표 모르는 jar 는 system scope, 인수인계에 install-file 명령). Gradle·빌드 파일 없음·합성 실패는 unsupported-build-tool 로 거부되고, pom.xml 도 build.xml 도 없는 트리는 sbmig inventory 로 설명서를 만들어 담당팀에 보냅니다. " +
    "pom 정리 순서: 떠도는 pom 복사본 무시(stray-pom-ignored, 커밋 없음) → 모듈이 트리에 없는 EAR 루트는 거부(ear-without-modules, 종료 코드 3) → 루트 pom 이 없으면 집계 pom 생성(aggregator-generated) → reactor 중복 모듈 제거(reactor-duplicate-removed) → 배포 전송 빌드 확장 제거(deploy-extensions-removed). " +
    "마지막으로 mvn help:effective-pom 으로 as-is 관리 버전을 logs/effective-depmgmt.json 에 저장합니다(실패해도 경고만, 나중에 버전 이어받기와 '바뀐 버전' 표에 씀).",
  lines: [
    "원본은 건드리지 않습니다. 사본을 git 저장소로 만들고 이후 모든 변경을 커밋으로 남깁니다",
    "Ant 프로젝트는 build.xml 로 pom.xml 을 만들어 받고, Gradle·빌드 파일 없는 폴더는 받지 않습니다",
    "거부되면 종료 코드 3 으로 끝나고 이유가 보고서에 남습니다. 변환·LLM 은 돌지 않습니다",
  ],
  draw(s, H) {
    const w = 1.55, g = (9 - 5 * w) / 4, h = 0.8, y1 = 1.5, y2 = 3.2, mid = 2.75;
    const col = i => H.AREA.x + i * (w + g);
    const o = { descSize: 9 };
    const a = H.node(s, col(0), y1, w, h, "사본 만들기", "원본은 두고 복사해 git 저장소로", "auto", o);
    const b = H.node(s, col(1), y1, w, h, "사전 점검", "Maven 이 도나?\nJDK 가 21 인가?", "ask", o);
    const c = H.node(s, col(2), y1, w, h, "빌드 파일은?", "pom.xml 이 있으면 그대로 다음으로", "ask", o);
    const d = H.node(s, col(3), y1, w, h, "pom 정리", "복사본 pom 무시, 루트 pom 없으면 생성", "auto", o);
    const e = H.node(s, col(4), y1, w, h, "원래 버전 기록", "Maven 으로 원래\n버전표를 저장", "build", o);
    const r = H.node(s, col(0), y2, 2 * w + g, h, "접수 거부", "종료 코드 3, 보고서에 이유를 적고 끝냅니다", "loop", o);
    const ant = H.node(s, col(2), y2, w, h, "pom.xml 만들기", "build.xml 과 jar 로 써서 커밋", "auto", o);
    const f = H.node(s, col(4), y2, w, h, "③ 분석으로", "바꾼 것은 모두 커밋으로", "done", o);
    // 본선
    H.seg(s, a.r, a.cy, b.l, b.cy, { head: true });
    H.seg(s, b.r, b.cy, c.l, c.cy, { head: true });
    H.seg(s, c.r, c.cy, d.l, d.cy, { head: true });
    H.seg(s, d.r, d.cy, e.l, e.cy, { head: true });
    H.seg(s, e.cx, e.b, f.cx, f.t, { head: true });
    // 사전 점검 실패 → 거부
    H.seg(s, b.cx, b.b, b.cx, r.t, { head: true });
    H.label(s, b.cx, mid, "실패", { w: 0.45 });
    // Gradle·빌드 파일 없음 → 거부
    const gx = c.l + 0.25, rx = r.r - 0.25;
    H.route(s, [[gx, c.b], [gx, mid], [rx, mid], [rx, r.t]]);
    H.label(s, (gx + rx) / 2 + 0.05, mid, "그 밖", { w: 0.45 });
    // build.xml 만 → pom.xml 만들기 → pom 정리
    const ax = c.cx + 0.25;
    H.seg(s, ax, c.b, ax, ant.t, { head: true });
    H.label(s, ax, mid, "build.xml 만", { w: 0.95 });
    H.route(s, [[ant.r, ant.cy], [d.cx, ant.cy], [d.cx, d.b]]);
  },
};
