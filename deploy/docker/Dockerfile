# sbmig 이미지 하나: Python 3.11 + JDK 21 + Maven + git + 앱 코드. 에이전트가 mvn / OpenRewrite 를 직접 돌린다 — 운영자 PC 와 같다
# (빌드-수정 루프가 시도마다 mvn 을 부르므로 같은 컨테이너의 Maven 이 가장 단순하고 빠르다).
# 빌드(sbmig 체크아웃 루트에서):   docker build -f deploy/docker/Dockerfile -t sbmig:<tag> .
# (회사 레포에서는 체크아웃이 src/migration-agent/ 이므로 그 디렉터리에서 빌드하거나 COPY 두 줄을 `COPY src/migration-agent/… …` 로)
ARG PYTHON_IMAGE=python:3.11-slim-trixie
FROM ${PYTHON_IMAGE}

# 사내 미러(선택): --build-arg PIP_INDEX_URL=https://nexus.company/repository/pypi/simple
#                   --build-arg PIP_TRUSTED_HOST=nexus.company
ARG PIP_INDEX_URL=
ARG PIP_TRUSTED_HOST=

RUN apt-get update && apt-get install -y --no-install-recommends \
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
COPY pyproject.toml deploy/docker/pip-deps.py /tmp/
RUN python /tmp/pip-deps.py /tmp/pyproject.toml \
    | xargs pip install ${PIP_INDEX_URL:+--index-url "$PIP_INDEX_URL"} ${PIP_TRUSTED_HOST:+--trusted-host "$PIP_TRUSTED_HOST"} \
    && rm -f /tmp/pyproject.toml /tmp/pip-deps.py

# 앱: config/, knowledge/, sbmig/ 가 같은 루트에 있어야 한다 (config.py 가 자기 위치에서 레포 루트를 잡는다 —
# pip install 로 site-packages 에 넣으면 config/ 와 knowledge/ 를 못 찾으므로 /app 에 두고 PYTHONPATH 로 돈다)
COPY --chown=sbmig:sbmig . /app/
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
