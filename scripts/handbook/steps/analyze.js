// ③ 분석: pom·소스를 읽어 종류를 정하고, ADF·속 빈 리액터·사내 프레임워크·루트 pom 을 가린다. 프로젝트는 바꾸지 않는다.
module.exports = {
  title: "③ 분석: 프로젝트 종류를 보고 어디까지 진행할지 정합니다",
  notes: "분석 단계는 프로젝트를 읽기만 하고 아무것도 바꾸지 않습니다. 모든 pom 을 직접 읽어 모듈 구성과 의존성, 버전을 파악하고, 소스에서는 javax import, web.xml, JSP, Spring XML 설정, 소스 파일의 문자 인코딩, 트리 안에 들어 있는 jar 같은 것을 훑습니다. 여기서 정리한 내용이 뒤의 모든 단계와 보고서의 바탕이 됩니다. " +
    "프로젝트 종류는 네 가지 중 하나로 정합니다. Spring Boot, Spring, 서블릿, 순수 자바 순으로 먼저 해당하는 것을 고르고, 배포할 서비스가 둘 이상이면 MSA 로 봅니다. 이 종류에 따라 다음 계획 단계에서 적용할 변환과 구조 정리 방식이 달라집니다. " +
    "Oracle ADF(JDeveloper) 프로젝트는 회사 결정에 따라 변환하지 않습니다. ADF 의 흔적을 네 가지 측면에서 찾아보고, 두 가지 이상에서 나오면 'ADF 프로젝트라 변환이 불가능합니다' 로 여기서 멈춥니다. 한 가지만 보이면 보고서에 메모만 남기고 계속 진행합니다. " +
    "pom 이 가리키는 모듈 안에는 소스가 없고, 실제 소스는 트리의 다른 곳에 있는 경우가 있습니다. 이런 프로젝트는 끝까지 진행하지만, 빌드가 통과해도 실제 코드가 빌드에 들어가지 않았으므로 성공으로 치지 않고 보고서의 「확인해 주세요」에서 알려 드립니다. " +
    "사내 프레임워크를 쓰는 프로젝트인지도 여기서 봅니다. 프로젝트 pom 에 직접 적혀 있는지, 부모 pom 에서 물려받는지, 트리 안 jar 로 들어 있는지를 보고, 그래도 못 찾으면 다른 라이브러리를 통해 들어오는지 Maven 으로 한 번 더 확인합니다. 찾으면 이 프로젝트는 JDK 21 과 jakarta 전환만 하도록 표시하고, 실제 범위는 다음 계획 단계에서 정합니다. 보고서에는 사내 프레임워크 프로젝트라는 안내가 따로 붙습니다. " +
    "pom 에 선언되지 않은 채 트리에 들어 있는 jar 는 목록으로 남겨 두고, 실제 연결은 구조 정리와 수정 단계에서 합니다. 수명이 끝난 라이브러리를 쓰고 있어도 진행을 막지는 않고, 수정 단계의 참고 자료로 쓰고 보고서의 「확인해 주세요」에 할 일과 함께 적습니다. " +
    "마지막으로 루트 pom.xml 이 없으면 Maven 이 빌드를 시작할 수 없으므로, MSA 가 아닌 한 여기서 멈춥니다. 멈춘 경우에는 변환이나 LLM 은 돌지 않고, 화면과 보고서에 이유가 나옵니다.",
  lines: [
    "분석은 프로젝트를 읽기만 합니다. 종류(Spring Boot, Spring, 서블릿, 순수 자바, MSA)를 정해 다음 단계에 넘깁니다.",
    "ADF 프로젝트는 변환하지 않습니다. 근거가 충분하면 'ADF 프로젝트라 변환이 불가능합니다' 로 여기서 멈춥니다.",
    "사내 프레임워크를 쓰면 JDK 21 과 jakarta 전환만 하도록 표시합니다. 수명이 끝난 라이브러리는 보고서의 「확인해 주세요」에 적습니다.",
  ],
  draw(s, H) {
    const w = 1.55, g = (9 - 5 * w) / 4, h = 0.8, y1 = 1.5, y2 = 3.2, mid = 2.75;
    const col = i => H.AREA.x + i * (w + g);
    const o = { descSize: 9 };
    const a = H.node(s, col(0), y1, w, h, "pom·소스 읽기", "모듈, 의존성, javax,\nJSP, 인코딩, jar", "auto", o);
    const c = H.node(s, col(1), y1, w, h, "종류 정하기", "Boot, Spring, 서블릿, 순수 자바 또는 MSA", "auto", o);
    const d = H.node(s, col(2), y1, w, h, "ADF 인가?", "흔적이 조금뿐이면 메모만 남기고 계속", "ask", o);
    const e = H.node(s, col(3), y1, w, h, "소스가 모듈 밖에?", "계속하지만 성공으로 치지 않음", "ask", o);
    const fw = H.node(s, col(4), y1, w, h, "사내 프레임워크?", "쓰면 JDK 21·jakarta\n전환만 하도록 표시", "ask", o);
    const f = H.node(s, col(4), y2, w, h, "jar·라이브러리 메모", "선언 안 된 jar,\n수명이 끝난 라이브러리", "auto", o);
    const gq = H.node(s, col(3), y2, w, h, "루트 pom.xml?", "없고 MSA 도 아니면\n멈춥니다", "ask", o);
    const hd = H.node(s, col(2), y2, w, h, "④ 계획으로", "분석 결과를 넘깁니다", "done", o);
    const st = H.node(s, col(0), y2, 2 * w + g, h, "여기서 멈춤", "화면과 보고서에 이유가 나옵니다. 변환과 LLM 은 돌지 않습니다", "loop", o);
    // 본선: 위 줄은 오른쪽으로, 아래 줄은 왼쪽으로
    H.seg(s, a.r, a.cy, c.l, c.cy, { head: true });
    H.seg(s, c.r, c.cy, d.l, d.cy, { head: true });
    H.seg(s, d.r, d.cy, e.l, e.cy, { head: true });
    H.seg(s, e.r, e.cy, fw.l, fw.cy, { head: true });
    H.seg(s, fw.cx, fw.b, f.cx, f.t, { head: true });
    H.seg(s, f.l, f.cy, gq.r, gq.cy, { head: true });
    H.seg(s, gq.l, gq.cy, hd.r, hd.cy, { head: true });
    // 멈춤 두 갈래: ADF 근거 충분, 루트 pom 없음 → 한 선으로 모여 멈춤으로
    const dx = d.l + 0.3, sx = st.r - 0.5;
    H.route(s, [[dx, d.b], [dx, mid], [sx, mid], [sx, st.t]]);
    H.seg(s, gq.cx, gq.t, gq.cx, mid);
    H.seg(s, gq.cx, mid, dx, mid);
    H.label(s, dx, (d.b + mid) / 2, "ADF 맞음", { w: 0.6 });
    H.label(s, gq.cx, (mid + gq.t) / 2, "없음", { w: 0.45 });
  },
};
