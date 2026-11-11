// ⑥ 구조 정리: 프로젝트 종류로 갈래를 고른 뒤, 모든 갈래가 같은 순서로 pom 을 회사 규칙에 맞추는 흐름.
module.exports = {
  title: "⑥ 구조 정리: 회사 표준 모듈과 pom 으로 바꾼다",
  notes: "구조 정리는 LLM 없이 규칙으로만 움직입니다(sbmig/restructure/restructurer.py 의 Restructurer.apply, 모드 layout / boot_pom / msa_pom). " +
    "비 Boot(Spring Framework·서블릿·순수 Java)는 restructure.apply 로 {name}-boot(생성한 Application 과 Boot 설정만) · {name}-back(as-is 소스 전부, 컨트롤러 포함) · " +
    "웹 자원이 있을 때만 {name}-front 로 나누고, JSP 가 있으면 boot 모듈을 실행 가능한 WAR 로(layout.jsp_boot_packaging: war), web.xml 의 서블릿·필터·리스너는 생성 클래스 WebXmlRegistrations 로 옮깁니다. " +
    "Spring Boot as-is 는 restructure.boot_pom(layout.boot_as_is: in-place), MSA 는 restructure.msa_pom, 소스 없는 pom 전용 프로젝트는 restructure.pom_only 로 디렉토리를 그대로 두고 pom 만 고치며, 모듈 없는 단일 Boot 앱은 {name}-boot 로 감쌉니다. " +
    "pom 작업 순서: 사내 parent / boot-jar / comp-jar 와 필수 2개 → 선택 아티팩트(이미 쓰는 것만, when_present 트리거면 추가) → 필터링 리소스 바이너리의 nonFilteredFileExtensions → " +
    "as-is effective pom 의 버전 고정(_pin_asis_versions) → 새 부모 체인이 관리하는 버전·속성·자기 dependencyManagement 항목 삭제 → 동봉 jar 연결(layout.wire_bundled_jars) → " +
    "as-is provided scope 복원과 소스 인코딩 → JSP javax → jakarta(layout.jsp_jakarta_rename), 단계마다 git 커밋. 이미 회사 구조인 프로젝트는 pom 정리만 합니다. " +
    "실패하면(ok=False 또는 예외) 플래그 restructure-failed 로 남은 재구성 단계(구조 검토 포함)를 건너뛰고 검증 빌드만 돌며, 빌드가 통과해도 성공이 아닙니다(종료 코드 2).",
  lines: [
    "비 Boot 는 boot·back·front 로 나누고, JSP 가 있으면 boot 모듈을 실행 가능한 WAR 로 만듭니다",
    "필터링 리소스의 바이너리 보호와 JSP 의 javax → jakarta 이름 바꾸기도 이 단계에서 합니다",
    "버전은 추측하지 않습니다: 옛 빌드가 쓰던 값이나 새 부모가 관리하는 값만 씁니다",
  ],
  draw(s, H) {
    const h = 0.72, ys = [1.45, 2.45, 3.43];
    const w0 = 1.35, w = 1.475, gap = 0.35;
    const xs = [0.5, 2.55]; for (let i = 2; i < 5; i++) xs.push(xs[i - 1] + w + gap);
    const at = (c, r, title, desc, kind) => H.node(s, xs[c], ys[r], c === 0 ? w0 : w, h, title, desc, kind);
    const ask = at(0, 0, "프로젝트 종류?", "분석 결과로 갈래를 고릅니다", "ask");
    const split = at(1, 0, "모듈 나누기", "소스는 back, 웹 자원은 front, Boot 설정은 boot", "auto");
    const keep = at(0, 1, "폴더는 그대로", "pom 만 고칩니다", "auto");
    const parent = at(2, 0, "부모 바꾸기", "루트는 사내 parent, 모듈은 boot-jar / comp-jar", "auto");
    const libs = at(3, 0, "사내 라이브러리", "필수 2개를 넣고, 선택은 쓰던 것만 올립니다", "auto");
    const vers = at(4, 0, "버전 정리", "옛 부모가 주던 버전은 적고, 새 부모와 겹치면 지웁니다", "auto");
    const jars = at(4, 1, "동봉 jar 연결", "pom 에 없던 jar 를 첫 빌드 전에 잇습니다", "auto");
    const ok = at(3, 1, "모두 적용됐나?", "단계마다 git 커밋을 남깁니다", "ask");
    const done = at(2, 1, "⑦ 구조 검토로", "바뀐 pom 을 LLM 이 점검합니다", "done");
    const fail = at(3, 2, "검토 없이 빌드만", "성공 아님, 종료 코드 2", "loop");
    const hz = (p, q) => H.seg(s, p.r, p.cy, q.l, q.cy, { head: true });
    // 갈래: 비 Boot → 나누기, Boot / MSA → 폴더 그대로
    hz(ask, split); H.label(s, (ask.r + split.l) / 2, ask.cy, "비 Boot", { w: 0.46 });
    H.seg(s, ask.cx, ask.b, keep.cx, keep.t, { head: true }); H.label(s, ask.cx + 0.42, (ask.b + keep.t) / 2, "Boot · MSA", { w: 0.75 });
    // 두 갈래가 같은 pom 작업으로 모인다
    hz(split, parent);
    const xg = (split.r + parent.l) / 2;
    H.route(s, [[keep.r, keep.cy], [xg, keep.cy], [xg, split.cy]], { head: false });
    hz(parent, libs); hz(libs, vers);
    H.seg(s, vers.cx, vers.b, jars.cx, jars.t, { head: true });
    H.seg(s, jars.l, jars.cy, ok.r, ok.cy, { head: true });
    H.seg(s, ok.l, ok.cy, done.r, done.cy, { head: true }); H.label(s, (ok.l + done.r) / 2, ok.cy - 0.2, "예", { w: 0.3 });
    H.seg(s, ok.cx, ok.b, fail.cx, fail.t, { head: true }); H.label(s, ok.cx + 0.29, (ok.b + fail.t) / 2, "아니오", { w: 0.5 });
  },
};
