// ⑥ 구조 정리: 프로젝트 종류로 갈래를 고른 뒤, 모든 갈래가 같은 순서로 pom 을 회사 규칙에 맞추는 흐름. LLM 없이 규칙으로만.
module.exports = {
  title: "⑥ 구조 정리: 회사 표준 모듈과 pom.xml로 바꿉니다",
  notes: "구조 정리는 LLM을 쓰지 않고 정해진 규칙대로만 진행합니다. " +
    "Spring Boot가 아닌 프로젝트(Spring Framework, 서블릿, 순수 Java)는 루트 바로 아래에 모듈 세 개를 만듭니다. " +
    "boot 모듈에는 새로 만든 시작 클래스와 Boot 설정만 두고, back 모듈에는 컨트롤러를 포함한 기존 소스를 모두 옮기고, 웹 자원이 있을 때만 front 모듈을 만들어 정적 파일과 JSP를 둡니다. " +
    "JSP가 있으면 boot 모듈을 java -jar로 실행되는 WAR로 만들고, 없으면 실행 가능한 jar로 만듭니다. web.xml에 있던 서블릿·필터·리스너는 자바 설정 클래스로 옮겨 적습니다. " +
    "Spring Boot 프로젝트도 모듈이 하나뿐이면(루트 pom.xml이 곧 앱이면) 다른 프로젝트와 같은 세 모듈로 나눕니다. boot 모듈에는 원래 시작 클래스와 Boot 설정만 두고, 나머지 소스는 back 모듈로, 정적 파일·JSP·템플릿은 front 모듈로 옮깁니다. 화면 주소와 뷰 이름은 그대로입니다. " +
    "모듈이 여럿인 Spring Boot 프로젝트와 MSA는 폴더 구조를 그대로 두고 pom.xml만 고칩니다. 이미 회사 구조로 바뀐 프로젝트도 pom.xml 정리만 합니다.\n\n" +
    "pom.xml은 모든 갈래가 같은 순서로 고칩니다. 먼저 루트는 사내 parent를, boot 모듈은 boot-jar를, 나머지 모듈은 comp-jar를 부모로 지정합니다. " +
    "다음으로 사내 필수 라이브러리 두 개를 넣고, 선택 라이브러리는 이미 쓰고 있는 것만 회사 버전으로 맞춥니다. 특정 라이브러리를 쓰면 대신 들어가도록 정해 둔 사내 라이브러리는 그 자리에 넣습니다.\n\n" +
    "의존성 버전은 추측하지 않습니다. 새 부모 pom이 버전을 정해 주면 pom.xml에서 버전을 지우고, 보고서에는 '부모 pom에서 지정'으로 나옵니다. " +
    "부모 pom에는 없고 Spring Boot BOM에 있으면 그 버전을 씁니다(기존 버전이 더 새것이면 낮추지 않습니다). " +
    "둘 다 없으면 기존 빌드가 쓰던 버전을 그대로 유지하고, 보고서의 「확인해 주세요」에 Spring Boot 4에서 동작하는지 확인해 달라고 적습니다. " +
    "기존 빌드에서도 버전을 알 수 없으면 저장소에 있는 가장 최신 정식 버전을 쓰고, 역시 「확인해 주세요」에 그 버전이 맞는지 확인을 요청합니다. " +
    "그마저 없으면 버전을 비워 두고, 버전을 적어 달라고 요청합니다. 새 부모 pom과 겹치는 옛 버전 속성도 이때 지웁니다.\n\n" +
    "마지막으로 첫 빌드 전에 손질할 것들을 처리합니다. pom.xml에 선언되지 않은 채 프로젝트에 들어 있던 jar(WEB-INF/lib의 암호화·SSO jar 같은 것)를 pom.xml에 연결하고, " +
    "리소스 필터링이 켜진 폴더의 바이너리 파일이 깨지지 않도록 막고, 소스 파일 인코딩을 pom.xml에 적고, 변환 때문에 compile로 바뀐 servlet API 같은 의존성을 원래의 provided로 되돌리고, " +
    "OpenRewrite가 읽지 않는 JSP 안의 javax 이름을 jakarta로 바꿉니다. 저장소에 없는 jar는 프로젝트 안에 두고 연결하므로, 사내 저장소(Nexus)에 올려 달라고 보고서에 적어 드립니다. " +
    "단계마다 git에 커밋을 남기므로 무엇이 바뀌었는지 하나씩 따라가 볼 수 있습니다.\n\n" +
    "구조 정리가 실패하면 구조 검토를 건너뛰고 기존 구조 그대로 빌드만 해 봅니다. 이때는 빌드가 통과해도 전환이 끝난 것으로 보지 않으며, 보고서의 「확인해 주세요」에 이유를 적습니다. " +
    "사내 프레임워크를 쓰는 프로젝트는 이 단계를 하지 않습니다. 모듈 구조와 부모 pom을 그대로 둡니다.",
  lines: [
    "Spring Boot가 아닌 프로젝트는 boot·back·front 모듈로 나누고, JSP가 있으면 WAR로 만듭니다. 모듈이 하나인 Spring Boot 프로젝트도 같은 세 모듈로 나누고, 모듈이 여럿이면 폴더를 그대로 둡니다.",
    "버전은 추측하지 않습니다. 부모 pom이 정하지 않으면 기존 버전을 유지하고 「확인해 주세요」에 적습니다.",
    "사내 프레임워크 프로젝트는 이 단계를 건너뛰고, 모듈 구조와 부모 pom을 그대로 둡니다.",
  ],
  draw(s, H) {
    const h = 0.72, ys = [1.45, 2.45, 3.43];
    const w0 = 1.35, w = 1.475, gap = 0.35;
    const xs = [0.5, 2.55]; for (let i = 2; i < 5; i++) xs.push(xs[i - 1] + w + gap);
    const at = (c, r, title, desc, kind) => H.node(s, xs[c], ys[r], c === 0 ? w0 : w, h, title, desc, kind);
    const ask = at(0, 0, "프로젝트 종류?", "분석 결과로 갈래를 고릅니다", "ask");
    const split = at(1, 0, "모듈 나누기", "소스는 back, 웹 자원은 front, Boot 설정은 boot", "auto");
    const keep = at(0, 1, "폴더는 그대로", "pom.xml만 고칩니다", "auto");
    const parent = at(2, 0, "부모 pom 바꾸기", "루트는 사내 parent, 모듈은 boot-jar / comp-jar", "auto");
    const libs = at(3, 0, "사내 라이브러리", "필수 2개를 넣고, 선택은 쓰던 것만 맞춥니다", "auto");
    const vers = at(4, 0, "버전 정하기", "부모 pom이 정하면 비우고, 아니면 기존 버전 유지", "auto");
    const prep = at(4, 1, "빌드 전 손질", "포함된 jar 연결, 인코딩, JSP의 javax 이름", "auto");
    const ok = at(3, 1, "모두 적용됐나?", "단계마다 git에 커밋합니다", "ask");
    const done = at(2, 1, "⑦ 구조 검토로", "바뀐 pom.xml을 LLM이 점검합니다", "done");
    const fail = at(3, 2, "검토 없이 빌드만", "통과해도 전환 완료로 보지 않습니다", "loop");
    const hz = (p, q) => H.seg(s, p.r, p.cy, q.l, q.cy, { head: true });
    // 갈래: Boot 아님·모듈 하나인 Boot → 나누기, 모듈 여럿인 Boot / MSA → 폴더 그대로
    hz(ask, split); H.label(s, (ask.r + split.l) / 2, ask.cy, "그 밖", { w: 0.45 });
    H.seg(s, ask.cx, ask.b, keep.cx, keep.t, { head: true }); H.label(s, ask.cx + 0.75, (ask.b + keep.t) / 2, "모듈 여럿인 Boot · MSA", { w: 1.3 });
    // 두 갈래가 같은 pom 작업으로 모인다
    hz(split, parent);
    const xg = (split.r + parent.l) / 2;
    H.route(s, [[keep.r, keep.cy], [xg, keep.cy], [xg, split.cy]], { head: false });
    hz(parent, libs); hz(libs, vers);
    H.seg(s, vers.cx, vers.b, prep.cx, prep.t, { head: true });
    H.seg(s, prep.l, prep.cy, ok.r, ok.cy, { head: true });
    H.seg(s, ok.l, ok.cy, done.r, done.cy, { head: true }); H.label(s, (ok.l + done.r) / 2, ok.cy - 0.2, "예", { w: 0.3 });
    H.seg(s, ok.cx, ok.b, fail.cx, fail.t, { head: true }); H.label(s, ok.cx + 0.29, (ok.b + fail.t) / 2, "아니오", { w: 0.5 });
  },
};
