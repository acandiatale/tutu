// ④ 계획: 분석이 읽은 프로젝트 종류(와 사내 프레임워크 여부)에 따라 레시피와 구조 정리 방식이 갈리는 결정 나무(LLM 없음). Hystrix 는 어느 종류든 Resilience4j 레시피를 더한다.
module.exports = {
  title: "④ 계획: 분석 결과에 맞춰 변환 규칙과 구조를 정합니다",
  notes: "계획은 LLM 없이 정해진 규칙으로 만듭니다. 그래서 분석 결과가 같으면 언제나 같은 계획이 나오고, 계획은 실행 폴더에 파일로 남아 나중에 무엇을 하기로 했는지 확인할 수 있습니다. " +
    "자동 변환에는 OpenRewrite 레시피를 씁니다. 현업과 정한 원칙은 '무엇을 바꿀지 모르는 레시피는 넣지 않는다' 입니다. 그래서 Java 21, javax 에서 jakarta 로의 전환, Spring 과 Spring Boot 판올림만 레시피로 하고, 그 밖의 문제는 빌드 오류로 드러나면 수정 단계에서 고칩니다. " +
    "Spring Boot 프로젝트는 Boot 4 레시피 하나가 jakarta 전환과 Spring 7, Security, Batch, Hibernate 까지 함께 처리합니다. Boot 가 아닌 Spring 프로젝트는 jakarta 와 Spring 7 레시피를 받고, Spring Security, Spring Batch, Hibernate 레시피는 그 라이브러리를 실제로 쓸 때만 더합니다. 서블릿 프로젝트는 jakarta 전환과 함께 Servlet 6 에서 없어진 API 정리를 받고, 순수 자바 프로젝트는 Java EE 의 javax 를 쓸 때만 jakarta 전환을 받습니다. 자바 소스가 없는 프로젝트는 변환 단계 없이 pom 만 고칩니다. " +
    "Hystrix 는 지원이 끝난 라이브러리라 Spring Boot 4 에서 동작하지 않습니다. 분석에서 Hystrix 가 보이면 프로젝트 종류와 상관없이(범위를 줄인 사내 프레임워크 프로젝트는 빼고) Hystrix 를 Resilience4j 로 바꾸는 레시피를 더합니다. @HystrixCommand 는 @CircuitBreaker 로 바뀌고, Hystrix 스타터는 Resilience4j 스타터로, 나머지 Hystrix 라이브러리는 빠집니다. 새 스타터의 버전은 레시피가 정하지 않고 새 부모 pom 과 Spring Cloud BOM 이 정합니다. 레시피로 옮기지 못하는 코드(HystrixCommand 를 상속한 클래스 등)는 빌드 오류로 드러나 수정 단계에서 옮기고, 동작 확인할 것은 보고서의 「확인해 주세요」에 적습니다. " +
    "구조는 회사 표준을 따릅니다. Spring Boot 프로젝트는 모듈이 여럿이면 디렉토리를 그대로 두고 pom 만 고치고, 모듈이 하나뿐이면 Boot 가 아닌 프로젝트와 같은 세 모듈로 나눕니다. Boot 가 아닌 프로젝트는 boot 와 back 모듈로 나누고, JSP 나 정적 파일 같은 웹 자원이 있으면 front 모듈을 더합니다. MSA 는 서비스마다 같은 규칙을 적용하되 공유 라이브러리를 먼저 처리하고, 디렉토리는 그대로 두고 pom 만 고칩니다. " +
    "사내 프레임워크 프로젝트는 범위를 줄여서 진행합니다. JDK 21 과 jakarta 레시피만 돌리고 JSP 안의 javax 이름도 jakarta 로 바꾼 뒤, 구조 정리와 구조 검토 없이 바로 빌드로 갑니다. 빌드는 한 번만 하고, 빌드 오류가 나도 고치지 않고 보고서에 남깁니다. 부모 pom 과 사내 프레임워크의 버전은 그대로 둡니다. 화면에는 구조 정리와 구조 검토가 '하지 않음' 으로 나오고, 화면과 보고서에는 '(프레임워크 이름) 프레임워크는 JDK 21, Jakarta만 적용합니다.' 라는 안내가 눈에 띄게 붙습니다. " +
    "어느 경우든 계획 끝에는 검증 빌드(mvn clean package)가 붙습니다. 일반 프로젝트는 그 앞에 LLM 구조 검토가 들어갑니다.",
  lines: [
    "LLM 없이 규칙으로 정합니다. 분석 결과가 같으면 언제나 같은 계획이 나오고, 계획은 파일로 남습니다.",
    "레시피는 Java 21, jakarta, Spring·Spring Boot 판올림만 씁니다. Hystrix 를 쓰고 있으면 Resilience4j 로 바꾸는 레시피를 더합니다.",
    "사내 프레임워크 프로젝트는 Java 21 과 jakarta 만 바꾸고, 구조 정리·검토 없이 빌드를 한 번만 합니다. 빌드 오류는 고치지 않습니다.",
  ],
  draw(s, H) {
    const A = H.AREA;
    const n = 5, rh = 0.46, gap = (A.h - n * rh) / (n - 1);
    const ry = i => A.t + i * (rh + gap);
    const ds = { descSize: 8 };
    // 왼쪽: 판단
    const q = H.node(s, A.l, A.t + 0.35, 1.5, 0.95, "어떤 프로젝트?", "종류, 쓰는 라이브러리, javax 사용을 보고 판단", "ask", { descSize: 9 });
    H.para(s, "Hystrix 를 쓰면 어느 종류든 Resilience4j 로 바꾸는 레시피를 더합니다.", A.l, q.b + 0.15, 1.45, 0.75, { size: 8.5, italic: true, color: "4A4F4A" });
    // 가운데: 레시피
    const rx = 3.4, rw = 2.35;
    const r = [
      H.node(s, rx, ry(0), rw, rh, "Java 21 + Boot 4", "Boot 4 레시피가 jakarta 까지 처리", "auto", ds),
      H.node(s, rx, ry(1), rw, rh, "Java 21 + jakarta + Spring 7", "Security·Batch·Hibernate 는 쓸 때만", "auto", ds),
      H.node(s, rx, ry(2), rw, rh, "Java 21 + jakarta", "순수 자바는 javax EE 를 쓸 때만 jakarta", "auto", ds),
      H.node(s, rx, ry(3), rw, rh, "레시피 없음", "자동 변환 단계를 건너뜁니다", "auto", ds),
      H.node(s, rx, ry(4), rw, rh, "Java 21 + jakarta 만", "프레임워크 버전과 부모 pom 은 그대로", "auto", ds),
    ];
    // 오른쪽: 구조 정리 방식
    const mx = 6.0, mw = 1.65;
    const m1 = H.node(s, mx, ry(0), mw, rh, "모듈 여럿: pom 만", "하나면 boot · back · front 로", "auto", ds);
    const m2 = H.node(s, mx, ry(1), mw, ry(2) + rh - ry(1), "모듈 나누기", "boot · back 으로 나누고 웹 자원이 있으면 front 도", "auto", { descSize: 8.5 });
    const m4 = H.node(s, mx, ry(3), mw, rh, "pom 만 고침", "나눌 모듈이 없습니다", "auto", ds);
    const m5 = H.node(s, mx, ry(4), mw, rh, "구조는 그대로", "구조 정리·검토 없음", "auto", ds);
    const t = H.node(s, 8.0, A.t + (A.h - 1.1) / 2, 1.5, 1.1, "계획 확정", "검증 빌드까지 순서를 정해 파일로 남깁니다", "done", { descSize: 9 });
    // 판단 → 다섯 갈래
    const tx = 2.1;
    H.seg(s, q.r, q.cy, tx, q.cy);
    H.seg(s, tx, r[0].cy, tx, r[4].cy);
    const names = ["Spring Boot", "Spring", "서블릿·순수 자바", "자바 소스 없음", "사내 프레임워크"];
    r.forEach((b, i) => { H.seg(s, tx, b.cy, b.l, b.cy, { head: true }); H.label(s, 2.75, b.cy, names[i], { w: 1.15 }); });
    // 레시피 → 구조 정리
    H.seg(s, r[0].r, r[0].cy, m1.l, r[0].cy, { head: true });
    H.seg(s, r[1].r, r[1].cy, m2.l, r[1].cy, { head: true });
    H.seg(s, r[2].r, r[2].cy, m2.l, r[2].cy, { head: true });
    H.seg(s, r[3].r, r[3].cy, m4.l, r[3].cy, { head: true });
    H.seg(s, r[4].r, r[4].cy, m5.l, r[4].cy, { head: true });
    // 구조 정리 → 계획 확정
    const ux = 7.83;
    [m1, m4, m5].forEach(m => H.seg(s, m.r, m.cy, ux, m.cy));
    H.seg(s, ux, m1.cy, ux, m5.cy);
    H.seg(s, m2.r, t.cy, t.l, t.cy, { head: true });
  },
};
