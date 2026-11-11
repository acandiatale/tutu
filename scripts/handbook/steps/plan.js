// ④ 계획: 분석이 읽은 프로젝트 종류에 따라 레시피와 구조 정리 방식이 갈리는 결정 나무(LLM 없음).
module.exports = {
  title: "④ 계획: 분석 결과로 레시피와 구조를 정한다",
  notes: "계획은 sbmig/plan/planner.py 의 build_plan 이 규칙으로 만들고 실행 폴더의 plan.json 에 저장합니다. " +
    "레시피 id 는 config/default.yml 의 openrewrite.recipes 세트에서 오고, 모두 knowledge/recipes/catalog.yml 에 있어야 합니다. " +
    "Spring(Boot 아님)은 UpgradeSpringFramework_7_0 을 늘 받고, Security 6_0 … 7_0 체인·SpringBatch5To6Migration·MigrateToHibernate71 은 분석기가 그 라이브러리를 찾았을 때만 받습니다. " +
    "서블릿 프로젝트는 JavaxMigrationToJakarta 에 RemovalsServletJakarta10 이 붙고, Boot 프로젝트는 UpgradeSpringBoot_4_0 + SpringBootProperties_4_1 우산 안에서 같은 일이 돕니다. " +
    "Boot 는 layout.boot_as_is: in-place(기본)라 pom 만 고치며 단일 모듈 Boot 앱은 {name}-boot 로 감쌉니다. plan.advisor 는 기본 none(LLM 이 레시피를 더하지 않음), plan.compile_after_rewrite 는 기본 false 라 변환 뒤 컴파일 단계도 없습니다.",
  lines: [
    "LLM 없이 규칙으로 정합니다. 같은 분석 결과면 언제나 같은 계획이 나오고 파일로 남습니다",
    "레시피는 Java 21 · jakarta · Spring/Boot 판올림만. 그 밖은 빌드 오류로 드러나 고칩니다",
    "MSA 는 서비스마다 같은 규칙으로(공유 라이브러리 먼저), 디렉토리는 그대로 두고 pom 만",
  ],
  draw(s, H) {
    const A = H.AREA;
    const rh = 0.53, gap = (A.h - 4 * rh) / 3;
    const ry = i => A.t + i * (rh + gap);
    // 왼쪽: 판단
    const q = H.node(s, A.l, A.t + (A.h - 0.95) / 2, 1.5, 0.95, "어떤 프로젝트?", "종류, 쓰는 라이브러리, javax 사용을 보고 판단", "ask", { descSize: 9 });
    // 가운데: 레시피
    const rx = 3.4, rw = 2.35;
    const r = [
      H.node(s, rx, ry(0), rw, rh, "Java 21 + Boot 4", "Boot 4 우산 레시피가 jakarta 까지", "auto"),
      H.node(s, rx, ry(1), rw, rh, "Java 21 + jakarta + Spring 7", "Security·Batch·Hibernate 는 쓸 때만", "auto"),
      H.node(s, rx, ry(2), rw, rh, "Java 21 + jakarta", "순수 자바는 javax EE 를 쓸 때만 jakarta", "auto"),
      H.node(s, rx, ry(3), rw, rh, "레시피 없음", "자동 변환 단계를 건너뜁니다", "auto"),
    ];
    // 오른쪽: 구조 정리 방식
    const mx = 6.0, mw = 1.65;
    const m1 = H.node(s, mx, ry(0), mw, rh, "pom 만 고침", "디렉토리는 그대로", "auto");
    const m2 = H.node(s, mx, ry(1), mw, ry(2) + rh - ry(1), "모듈 분리", "boot · back 으로 나누고 웹 자원이 있으면 front 도", "auto", { descSize: 9 });
    const m4 = H.node(s, mx, ry(3), mw, rh, "pom 만 고침", "나눌 모듈이 없습니다", "auto");
    const t = H.node(s, 8.0, A.t + (A.h - 1.1) / 2, 1.5, 1.1, "계획 확정", "구조 검토와 검증 빌드를 붙여 파일로 남깁니다", "done", { descSize: 9 });
    // 판단 → 네 갈래
    const tx = 2.1;
    H.seg(s, q.r, q.cy, tx, q.cy);
    H.seg(s, tx, r[0].cy, tx, r[3].cy);
    const names = ["Spring Boot", "Spring", "서블릿·순수 자바", "자바 소스 없음"];
    r.forEach((b, i) => { H.seg(s, tx, b.cy, b.l, b.cy, { head: true }); H.label(s, 2.75, b.cy, names[i], { w: 1.15 }); });
    // 레시피 → 구조 정리
    H.seg(s, r[0].r, r[0].cy, m1.l, r[0].cy, { head: true });
    H.seg(s, r[1].r, r[1].cy, m2.l, r[1].cy, { head: true });
    H.seg(s, r[2].r, r[2].cy, m2.l, r[2].cy, { head: true });
    H.seg(s, r[3].r, r[3].cy, m4.l, r[3].cy, { head: true });
    // 구조 정리 → 계획 확정
    const ux = 7.83;
    H.seg(s, m1.r, m1.cy, ux, m1.cy);
    H.seg(s, m4.r, m4.cy, ux, m4.cy);
    H.seg(s, ux, m1.cy, ux, m4.cy);
    H.seg(s, m2.r, t.cy, t.l, t.cy, { head: true });
  },
};
