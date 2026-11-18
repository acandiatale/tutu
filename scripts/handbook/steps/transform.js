// ⑤ 자동 변환: 계획한 변환 규칙을 OpenRewrite 한 번에 적용하고, 실패하면 방법을 바꿔 다시 실행한 뒤 커밋하는 흐름.
module.exports = {
  title: "⑤ 자동 변환: OpenRewrite로 코드를 한 번에 바꿉니다",
  notes: "이 단계에서는 OpenRewrite라는 자동 변환 도구가 프로젝트의 코드와 pom.xml을 직접 고칩니다. " +
    "Java 21 전환과 javax → jakarta 이름 바꾸기는 모든 대상 프로젝트에 적용하고, Spring Boot 프로젝트에는 Spring Boot 4 전환 규칙을, Spring Boot가 아닌 Spring 프로젝트에는 Spring Framework 7 전환 규칙을 더합니다. " +
    "Spring Boot가 아닌 Spring 프로젝트가 Spring Security, Spring Batch, Hibernate를 쓰고 있으면 그 전환 규칙도 함께 넣습니다(Spring Boot 4 전환 규칙에는 이미 들어 있습니다). " +
    "규칙은 모두 묶어서 한 번만 실행하고, 중간에 빌드하지 않습니다. 변환이 제대로 됐는지는 구조 정리가 끝난 뒤 ⑧ 검증 빌드에서 확인합니다.\n\n" +
    "라이브러리 몇 가지는 이 단계에서 함께 바뀝니다. Hystrix를 쓰는 프로젝트는 @HystrixCommand가 Resilience4j의 @CircuitBreaker로, Hystrix starter가 Resilience4j starter로 바뀝니다. " +
    "이때 버전은 새로 지어내지 않고, 다음 단계의 버전 정리 규칙을 그대로 따릅니다. HystrixCommand를 상속한 클래스처럼 규칙으로 옮길 수 없는 코드는 빌드 오류로 드러나고, 수정 루프에서 고칩니다. " +
    "Spring Boot 프로젝트의 Springfox는 springdoc으로, Sleuth starter는 Micrometer Tracing으로 바뀝니다. 바뀐 라이브러리는 보고서의 「확인해 주세요」에 실행 중에 확인할 점과 함께 적어 드립니다.\n\n" +
    "실행이 실패하면 원인에 따라 방법을 바꿔 한 번 더 실행합니다. 메모리가 부족하면 메모리를 늘려서, 라이브러리 다운로드에 실패하면 잠시 기다렸다가, 저장소의 오래된 실패 기록 때문이면 그 기록을 무시하고, 컴파일이 안 되는 프로젝트라 실패하면 컴파일 없이 다시 실행합니다. " +
    "그래도 실패하면 오류로 기록해 두고 다음 단계로 넘어갑니다. 실제 문제는 빌드에서 드러나기 때문입니다. " +
    "다만 사내 부모 pom을 저장소에서 받지 못하거나 Maven 설정이 http 저장소를 막고 있으면, 코드로 고칠 수 없는 문제이므로 실행을 멈추고 보고서에 이유를 적습니다. " +
    "프로젝트가 저장소에 없는 옛 버전의 사내 부모 pom을 쓰고 있으면, 회사가 정한 버전으로 맞춘 뒤 한 번 더 실행합니다. 사내 프레임워크 프로젝트는 부모 pom을 바꾸지 않으므로 이 경우에도 멈춥니다. " +
    "변환이 끝났는데 pom.xml에 4.0.x처럼 버전이 덜 정해진 글자가 남으면, 결과를 되돌리고 잠시 뒤 한 번 더 실행합니다.\n\n" +
    "JSP는 OpenRewrite가 읽지 않습니다. 그래서 JSP 안의 javax 이름은 다음 단계인 구조 정리에서 따로 바꿉니다. " +
    "사내 프레임워크를 쓰는 프로젝트는 범위를 줄여 Java 21과 jakarta 전환만 합니다. 이때는 구조 정리를 하지 않으므로, 구조 정리가 하던 손질 세 가지를 이 단계에서 합니다. " +
    "변환 전에 소스 파일 인코딩을 pom.xml에 적고, 변환 뒤에는 원래 provided였던 servlet API 같은 의존성을 다시 provided로 돌려놓고, JSP의 javax 이름을 바꿉니다.",
  lines: [
    "변환 규칙은 한 번에 묶어 한 번만 실행합니다. 결과는 ⑧ 검증 빌드에서 확인합니다.",
    "Hystrix는 Resilience4j로, Spring Boot 프로젝트의 Springfox·Sleuth는 springdoc·Micrometer Tracing으로 바뀝니다.",
    "사내 프레임워크 프로젝트는 Java 21과 jakarta 전환만 하고, 구조 정리와 구조 검토는 하지 않습니다.",
  ],
  draw(s, H) {
    const w = 1.5, gap = 0.375, x = c => 0.5 + c * (w + gap);
    const y1 = 1.5, h1 = 0.72, y2 = 2.52, h2 = 0.62, y3 = 3.42, h3 = 0.7;
    const a = H.node(s, x(0), y1, w, h1, "변환 규칙 고르기", "프로젝트 종류와 쓰는 라이브러리에 맞춰", "auto");
    const b = H.node(s, x(1), y1, w, h1, "OpenRewrite 실행", "코드와 pom.xml을 한 번에 바꿉니다", "build");
    const c = H.node(s, x(2), y1, w, h1, "끝까지 돌았나?", "실행이 성공으로 끝났는지", "ask");
    const d = H.node(s, x(3), y1, w, h1, "변경 저장", "바뀐 파일을 git에 커밋합니다", "auto");
    const e = H.node(s, x(4), y1, w, h1, "⑥ 구조 정리로", "중간 빌드 없이 넘어갑니다", "done");
    const r = H.node(s, x(2), y2, w, h2, "방법 바꿔 다시", "메모리·다운로드 문제 등", "loop");
    const lib = H.node(s, 0.5, y3, 4.4, h3, "함께 바뀌는 라이브러리",
      "Hystrix → Resilience4j, Springfox → springdoc, Sleuth → Micrometer Tracing", "auto");
    const fw = H.node(s, 5.1, y3, 4.4, h3, "사내 프레임워크 프로젝트",
      "Java 21과 jakarta만 바꾸고, 꼭 필요한 pom.xml·JSP 손질도 여기서 합니다", "ask");
    const hz = (p, q) => H.seg(s, p.r, p.cy, q.l, q.cy, { head: true });
    hz(a, b); hz(b, c); hz(d, e);
    hz(c, d); H.label(s, (c.r + d.l) / 2, c.cy - 0.2, "예", { w: 0.3 });
    // 실패 → 방법 바꿔 다시 → OpenRewrite 실행
    H.seg(s, c.cx, c.b, r.cx, r.t, { head: true });
    H.label(s, c.cx + 0.3, (c.b + r.t) / 2, "아니오", { w: 0.45 });
    H.route(s, [[r.l, r.cy], [b.cx, r.cy], [b.cx, b.b]]);
    H.label(s, (r.l + b.cx) / 2, r.cy - 0.17, "한 번 더", { w: 0.55 });
    // 그래도 실패 → 오류로 기록하고 계속
    H.route(s, [[r.r, r.cy], [d.cx, r.cy], [d.cx, d.b]]);
    H.label(s, d.cx, (d.b + r.cy) / 2 + 0.04, "그래도 실패: 기록하고 계속", { w: 1.6 });
  },
};
