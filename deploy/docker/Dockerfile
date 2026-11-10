# sbmig 이미지 하나: Python 3.11 + JDK 21 + Maven + git + 앱 코드. 에이전트가 mvn / OpenRewrite 를 직접 돌린다 — 운영자 PC 와 같다
# (빌드-수정 루프가 시도마다 mvn 을 부르므로 같은 컨테이너의 Maven 이 가장 단순하고 빠르다).
# 빌드(sbmig 체크아웃 루트에서):   docker build -f deploy/docker/Dockerfile -t sbmig:<tag> .
# 회사 레포(2026-11-09: 코드는 src/migration-agent/, deploy/ 는 레포 루트)는 루트에서 두 인자로 — 컨텍스트 루트에 .dockerignore 를 두어야 한다
# (견본 deploy/docker/root.dockerignore.example 을 루트에 .dockerignore 로 복사):
#   docker build --build-arg APP_DIR=src/migration-agent --build-arg DEPLOY_DIR=deploy -f deploy/docker/Dockerfile -t sbmig:<tag> .
# 회사 설정 config/artifacts.yml 은 이미지에 굽는다(2026-11-10) — git 이 무시하는 파일이라 새로 받은 체크아웃에는 없다. 없는 채로 빌드되면
# 회사 설정 없이 도는 이미지가 조용히 나오므로, 회사 빌드에는 이 인자를 붙여 없을 때 빌드를 멈춘다(빈칸으로 여러 이름):
#   --build-arg REQUIRE_CONFIG=artifacts.yml
# 폐쇄망 빌드 인자(전부 선택, 비우면 공개 저장소):
#   --build-arg PYTHON_IMAGE=<harbor>/<proxy project>/library/python:3.11-slim-trixie   베이스 이미지를 사내 Harbor 프록시에서
#   --build-arg APT_MIRROR=https://nexus.company/repository                              deb.debian.org 를 대신할 주소(뒤에 /debian, /debian-security 가 붙는다)
#   --build-arg PIP_INDEX_URL=https://nexus.company/repository/pypi/simple --build-arg PIP_TRUSTED_HOST=nexus.company
#   프록시로 나가는 망이면 --build-arg HTTP_PROXY=… HTTPS_PROXY=… NO_PROXY=… (docker 가 빌드 중에만 쓴다)
ARG PYTHON_IMAGE=python:3.11-slim-trixie
FROM ${PYTHON_IMAGE}

ARG APT_MIRROR=
ARG PIP_INDEX_URL=
ARG PIP_TRUSTED_HOST=
# 컨텍스트 안에서 sbmig 체크아웃(pyproject.toml, sbmig/, config/, knowledge/)과 deploy/ 가 있는 곳 — 우리 레포는 둘 다 루트(. / deploy)
ARG APP_DIR=.
ARG DEPLOY_DIR=deploy

RUN if [ -n "$APT_MIRROR" ]; then sed -i "s|http://deb.debian.org|${APT_MIRROR%/}|g" /etc/apt/sources.list.d/debian.sources; fi \
    && apt-get update && apt-get install -y --no-install-recommends \
        openjdk-21-jdk-headless maven git ca-certificates unzip curl \
    && rm -rf /var/lib/apt/lists/* \
    # /opt/java21 → 이 아키텍처의 JDK (amd64 / arm64 경로가 다르다); JAVA_HOME 은 여기를 가리킨다
    && ln -s "$(dirname "$(dirname "$(readlink -f "$(command -v javac)")")")" /opt/java21

# sbmig 은 JAVA21_HOME / JAVA_HOME 을 읽는다 (설정 build.java_home 이 있으면 그것이 우선)
ENV JAVA_HOME=/opt/java21 \
    JAVA21_HOME=/opt/java21 \
    PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app \
    HOME=/home/sbmig

# UID 1000: 데이터 볼륨의 파일 소유자를 모든 컨테이너에서 같게
RUN useradd --create-home --uid 1000 sbmig && mkdir -p /app /data /workspace && chown -R sbmig:sbmig /app /data /workspace

# 의존성 먼저(캐시 레이어); 목록은 pyproject.toml 그 자체
COPY ${APP_DIR}/pyproject.toml ${DEPLOY_DIR}/docker/pip-deps.py /tmp/
RUN python /tmp/pip-deps.py /tmp/pyproject.toml \
    | xargs pip install ${PIP_INDEX_URL:+--index-url "$PIP_INDEX_URL"} ${PIP_TRUSTED_HOST:+--trusted-host "$PIP_TRUSTED_HOST"} \
    && rm -f /tmp/pyproject.toml /tmp/pip-deps.py

# 앱: config/, knowledge/, sbmig/ 가 같은 루트에 있어야 한다 (config.py 가 자기 위치에서 레포 루트를 잡는다 —
# pip install 로 site-packages 에 넣으면 config/ 와 knowledge/ 를 못 찾으므로 /app 에 두고 PYTHONPATH 로 돈다)
COPY --chown=sbmig:sbmig ${APP_DIR}/ /app/
COPY --chown=sbmig:sbmig ${DEPLOY_DIR}/ /app/deploy/
# 이미지에 꼭 들어 있어야 하는 config/ 아래 파일 이름들(빈칸 구분; 비우면 검사하지 않는다). 회사 설정이 컨텍스트에 없으면 여기서 멈춘다.
# 인자를 여기서 선언하는 까닭: 값이 바뀌어도 위의 apt / pip 레이어 캐시가 깨지지 않게. 메시지는 Windows 콘솔에서 깨지지 않게 영문.
ARG REQUIRE_CONFIG=
RUN for f in $REQUIRE_CONFIG; do \
        test -f "/app/config/$f" || { echo "ERROR: config/$f is missing from the build context (REQUIRE_CONFIG). Put it at ${APP_DIR}/config/$f and build again." >&2; exit 1; }; \
    done
# Windows 체크아웃 방어: CRLF 로 바뀐 셸 스크립트는 컨테이너에서 못 돈다(`$'\r': command not found`) → LF 로 되돌리고 실행 권한을 보장한다
# (.gitattributes 가 LF 를 강제하지만, 이미 CRLF 로 받은 체크아웃도 있을 수 있다)
RUN sed -i 's/\r$//' /app/deploy/docker/*.sh && chmod 755 /app/deploy/docker/*.sh
WORKDIR /app
USER sbmig

# 상태는 볼륨에, 이미지 안에는 두지 않는다: 실행 디렉터리(--out), KB(회사 yml 의 kb.dir), LLM 기록
# /data = 웹 UI 에이전트(Deployment), /workspace = 요청별 Job(emptyDir)
VOLUME ["/data", "/home/sbmig/.m2"]
EXPOSE 8765

# `sbmig serve` 가 곧 uvicorn 이다(프로세스 안에서 uvicorn.run; 워커 하나 — 한 번에 한 이관, 큐가 프로세스 안에 있다).
# 루프백이 아닌 --host 는 SBMIG_WEB_TOKEN(Secret) 또는 SBMIG_WEB_OPEN=1(사내망 전용 페이지 — Deployment 의 env, 2026-11-05 결정)
# 둘 중 하나가 있어야 뜬다. 이미지에는 둘 다 없다. --graceful-timeout 은 종료 때 SSE 스트림을 닫는 여유.
# 요청별 Job 모델은 이 CMD 대신 /app/deploy/docker/run-job.sh 를 command 로 쓴다.
CMD ["python", "-m", "sbmig.cli", "serve", "--host", "0.0.0.0", "--port", "8765", "--out", "/data/out", \
     "--graceful-timeout", "30", "--max-upload-mb", "1024"]
