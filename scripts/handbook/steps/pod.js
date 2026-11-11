// 업로드가 빌드 파드(쿠버네티스 Job)가 되어 돌고 끝날 때까지: 줄 서기, 입력·환경 대조, 거절, 결과 파일, 자리 반납.
module.exports = {
  title: "① 파드 준비: 업로드마다 빌드 파드를 띄우고 거둔다",
  notes: "웹 파드(sbmig serve, SBMIG_WEB_MODE=k8s-job)는 업로드를 받으면 실행 폴더에 source.zip 과 control/request.json(zip sha256, 설정 파일 digest, sbmig 버전)을 고정하고 줄(_FifoTurns)에 세운다. " +
    "동시에 도는 빌드 Job 수는 SBMIG_JOB_MAX_PARALLEL(코드 기본 1, 배포 ConfigMap 값 3)이고, 2번 자리부터는 자기 Maven 로컬 저장소(<저장소>-slot<k>)를 쓴다. " +
    "Job 의 명령 python -m sbmig.web.worker 는 request.json 과 control/state.json 의 env_pins 를 대조해 다르면 거절(exit 3, job-status.json 에 이유)하고, 같으면 sbmig run 을 돌린 뒤 실행 폴더의 비밀 값을 지우고 job-status.json 을 쓴다. " +
    "빌드 실패(sbmig exit 2)여도 Worker 는 0 으로 끝나 파드는 Completed 이고, exit 3(거절·중단·ADF 정지 등)이면 Error 이다. 템플릿 81-web-job: backoffLimit 0, restartPolicy Never, activeDeadlineSeconds 14400, ttlSecondsAfterFinished 3600. " +
    "웹 파드의 _watch_job 은 Job 이 끝난 것을 API 로 확인한 뒤에야 자리를 넘기며, 멈춤은 stop.request 를 쓰고 유예가 지나면 자기 라벨의 Job 만 지운다.",
  lines: [
    "웹 파드는 늘 떠 있고, 업로드마다 일회용 빌드 파드를 하나씩 띄웁니다",
    "빌드가 실패해도 파드는 정상 종료합니다. 성공 여부는 보고서가 말합니다",
    "멈춤을 누르면 기다리던 건은 바로 빠지고, 도는 건은 단계 사이에서 멈춥니다",
  ],
  draw(s, H) {
    const w = 1.5, gx = 0.375, col = i => H.AREA.x + i * (w + gx);
    const r1 = 1.42, r2 = 2.45, r3 = 3.45, h = 0.7;
    const A = H.node(s, col(0), r1, w, h, "업로드 접수", "zip 을 공유 디스크에 두고 지문을 적어 둡니다", "auto");
    const B = H.node(s, col(1), r1, w, h, "빈 자리가 있나?", "빌드 파드는 동시에\n3개까지 (설정값)", "ask");
    const C = H.node(s, col(2), r1, w, h, "빌드 파드 띄우기", "웹 파드가 쿠버네티스\nJob 을 만듭니다", "auto");
    const Hd = H.node(s, col(4), r1, w, h, "자리 반납", "파드 종료를 확인한 뒤\n다음 차례에 넘깁니다", "auto");
    const W = H.node(s, col(1), r2, w, h, "줄에서 대기", "올린 순서대로.\n화면에 몇 번째인지 표시", "loop");
    const D = H.node(s, col(2), r2, w, h, "입력·환경 대조", "zip·설정·버전이\n접수 때와 같은가?", "ask");
    const E = H.node(s, col(3), r2, w, h, "이관 실행", "② 접수부터\n⑪ 마무리까지 차례로", "build");
    const R = H.node(s, col(2), r3, w, 0.68, "실행 거절", "이관하지 않습니다. 종료 코드 3", "loop");
    const F = H.node(s, col(3), r3, w, 0.68, "결과 파일 쓰기", "비밀 값을 지우고\n종료 코드를 디스크에", "auto");
    const G = H.node(s, col(4), r3, w, 0.68, "파드 종료", "Completed 또는 Error\n1시간 뒤 자동 삭제", "done");
    H.para(s, "빌드 파드 = 업로드 하나를 처리하고 끝나는 일회용 컨테이너.\n결과는 파드가 아니라 공유 디스크에 남습니다.",
      col(0), r2 + 0.05, w, 1.55, { size: 9, italic: true, color: "4A4F4A" });
    const hz = (p, q) => H.seg(s, p.r, p.cy, q.l, q.cy, { head: true });
    const above = (p, q, t, lw) => H.label(s, (p.r + q.l) / 2, p.cy - 0.17, t, { w: lw });
    hz(A, B);
    hz(B, C); above(B, C, "예", 0.3);
    // 없음 ↓ 대기, 자리가 나면 ↑ 다시 확인
    const mid = (B.b + W.t) / 2;
    H.seg(s, B.cx - 0.35, B.b, B.cx - 0.35, W.t, { head: true }); H.label(s, B.cx - 0.35, mid, "없음", { w: 0.38 });
    H.seg(s, B.cx + 0.35, W.t, B.cx + 0.35, B.b, { head: true }); H.label(s, B.cx + 0.35, mid, "자리가 나면", { w: 0.75 });
    H.seg(s, C.cx, C.b, D.cx, D.t, { head: true });
    hz(D, E); above(D, E, "같음", 0.34);
    H.seg(s, D.cx, D.b, R.cx, R.t, { head: true }); H.label(s, D.cx, (D.b + R.t) / 2, "다름", { w: 0.38 });
    H.seg(s, E.cx, E.b, F.cx, F.t, { head: true });
    hz(R, F); hz(F, G);
    H.seg(s, G.cx, G.t, Hd.cx, Hd.b, { head: true });
  },
};
