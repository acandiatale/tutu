// ③ 분석: pom·소스를 읽어 종류를 정하고, ADF·속 빈 리액터·루트 pom 으로 멈출지·성공 불가인지 가린다.
module.exports = {
  title: "③ 분석: 프로젝트 종류를 정하고 멈출지 가린다",
  notes: "분석은 프로젝트를 바꾸지 않고 profile.json 하나를 남깁니다(sbmig/analyze/analyzer.py analyze_project). pom 은 직접 파싱하고 Maven 을 부르지 않으며, 소스 스캔은 import(javax·jakarta), web.xml, JSP, Spring XML, 인코딩(sourceEncoding 없이 UTF-8 아닌 소스), 필터링되는 리소스 아래 바이너리, 외부 설정 키를 모읍니다. " +
    "종류는 classifier.py: 배포 서비스 루트가 둘 이상이면 MSA, 아니면 Boot > Spring > 서블릿 > 순수 Java 순서입니다. " +
    "ADF(sbmig/analyze/adf.py)는 BC4J XML, oracle.jbo/adf/binding import, ADF Model 바인딩, ADF Faces·task flow 네 계열을 보고, 두 계열 이상이면 intake.adf_policy: stop(기본)에서 플래그 adf-project + 인수인계 첫 줄 + 종료 코드 3, 한 계열이면 adf-traces 메모만 남깁니다. " +
    "속 빈 리액터(hollow.py)는 모듈 안 java 0 개·밖에 java 가 있을 때(또는 WAR 인데 JSP 가 밖에만 있을 때) 플래그 hollow-reactor 를 달고, 빌드가 통과해도 성공이 아니라 종료 코드 2 입니다. " +
    "동봉 jar(bundled_jars.py, intake.bundled_jars)는 logs/bundled-jars.json 에 적고 연결은 구조 정리와 수정 루프가 합니다. 권고(knowledge/libraries/advisories.yml)는 안내뿐이며, 루트 pom.xml 이 없고 MSA 도 아니면 no-root-pom 으로 종료 코드 3 입니다.",
  lines: [
    "ADF 근거가 두 계열 이상이면 여기서 끝납니다: 'ADF 프로젝트라 변환이 불가능합니다', 종료 코드 3",
    "소스가 모듈 밖에만 있으면 계속 돌지만, 빌드가 통과해도 성공으로 치지 않습니다(종료 코드 2)",
    "수명이 끝난 라이브러리는 막지 않습니다. 수정 힌트와 보고서 인수인계에만 적습니다",
  ],
  draw(s, H) {
    const w = 1.55, g = (9 - 5 * w) / 4, h = 0.8, y1 = 1.5, y2 = 3.2, mid = 2.75;
    const col = i => H.AREA.x + i * (w + g);
    const o = { descSize: 9 };
    const a = H.node(s, col(0), y1, w, h, "모든 pom 읽기", "모듈·의존성·버전\nMaven 은 부르지 않음", "auto", o);
    const b = H.node(s, col(1), y1, w, h, "소스·자원 훑기", "javax, web.xml, JSP, 인코딩, 트리 안 jar", "auto", o);
    const c = H.node(s, col(2), y1, w, h, "종류 정하기", "Boot, Spring, 서블릿, 순수 Java 또는 MSA", "auto", o);
    const d = H.node(s, col(3), y1, w, h, "ADF 인가?", "근거 한 계열뿐이면 메모만 남기고 계속", "ask", o);
    const e = H.node(s, col(4), y1, w, h, "속 빈 리액터?", "그렇다면 '성공 불가'로 표시하고 계속", "ask", o);
    const f = H.node(s, col(4), y2, w, h, "jar·권고 기록", "pom 에 없는 jar,\n수명 끝난 라이브러리", "auto", o);
    const gq = H.node(s, col(3), y2, w, h, "루트 pom.xml?", "없고 MSA 도 아니면\n멈춥니다", "ask", o);
    const hd = H.node(s, col(2), y2, w, h, "④ 계획으로", "분석 결과(프로필)를 넘깁니다", "done", o);
    const st = H.node(s, col(0), y2, 2 * w + g, h, "분석에서 중단", "종료 코드 3, 보고서에 이유. Maven·LLM 은 부르지 않습니다", "loop", o);
    // 본선: 위 줄은 오른쪽으로, 아래 줄은 왼쪽으로
    H.seg(s, a.r, a.cy, b.l, b.cy, { head: true });
    H.seg(s, b.r, b.cy, c.l, c.cy, { head: true });
    H.seg(s, c.r, c.cy, d.l, d.cy, { head: true });
    H.seg(s, d.r, d.cy, e.l, e.cy, { head: true });
    H.seg(s, e.cx, e.b, f.cx, f.t, { head: true });
    H.seg(s, f.l, f.cy, gq.r, gq.cy, { head: true });
    H.seg(s, gq.l, gq.cy, hd.r, hd.cy, { head: true });
    // 멈춤 두 갈래: ADF 두 계열 이상, 루트 pom 없음 → 한 선으로 모여 중단으로
    const dx = d.l + 0.3, sx = st.r - 0.5;
    H.route(s, [[dx, d.b], [dx, mid], [sx, mid], [sx, st.t]]);
    H.seg(s, gq.cx, gq.t, gq.cx, mid);
    H.seg(s, gq.cx, mid, dx, mid);
    H.label(s, dx, (d.b + mid) / 2, "두 계열 이상", { w: 0.9 });
    H.label(s, gq.cx, (mid + gq.t) / 2, "없음", { w: 0.45 });
  },
};
