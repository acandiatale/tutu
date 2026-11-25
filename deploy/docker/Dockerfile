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
#   PC 의 pip 는 받는데 빌드만 `SSLError` 로 멈추면 회사망이 HTTPS 를 검사하는 것이다(검사용 인증서가 PC 에는 있고 컨테이너에는 없다).
#   사내 PyPI 미러가 없으면 공개 호스트 둘을 믿게 한다 — 호스트 여러 개는 빈칸으로:
#   --build-arg "PIP_TRUSTED_HOST=pypi.org files.pythonhosted.org"
#   프록시로 나가는 망이면 --build-arg HTTP_PROXY=… HTTPS_PROXY=… NO_PROXY=… (docker 가 빌드 중에만 쓴다)
#   JDK(Amazon Corretto 21)를 사내 미러에서 받으려면 --build-arg CORRETTO_BASE_URL=<미러 주소>, HTTPS 검사 망에서 인증서 오류가 나면
#   --build-arg CORRETTO_TLS_VERIFY=0 을 준다. 자세한 내용은 아래 CORRETTO_ 인자의 설명에 있다.
ARG PYTHON_IMAGE=python:3.11-slim-trixie
FROM ${PYTHON_IMAGE}

ARG APT_MIRROR=
ARG PIP_INDEX_URL=
ARG PIP_TRUSTED_HOST=
# 컨텍스트 안에서 sbmig 체크아웃(pyproject.toml, sbmig/, config/, knowledge/)과 deploy/ 가 있는 곳 — 우리 레포는 둘 다 루트(. / deploy)
ARG APP_DIR=.
ARG DEPLOY_DIR=deploy
# JDK 는 Amazon Corretto 21 을 버전을 정해 받는다. Debian 의 openjdk 패키지는 빌드하는 날 apt 미러에 있는 빌드가 들어와서
# 어떤 빌드인지 이 파일에 남지 않는다. 21.0.13.1.2 는 Java4Shell(CVE-2026-114938)이 고쳐진 Corretto 빌드다.
# 받는 파일은 <CORRETTO_BASE_URL>/<CORRETTO_VERSION>/java-21-amazon-corretto-jdk_<CORRETTO_VERSION>-1_<amd64|arm64>.deb 이다.
# 설치하기 전에 SHA-256 을 아래 값과 비교하고 다르면 빌드를 멈춘다. 값은 GitHub corretto/corretto-21 의 해당 버전 릴리스 페이지에
# 있다. 버전을 바꿀 때는 CORRETTO_VERSION 과 두 체크섬을 함께 바꾼다.
# 폐쇄망에서 빌드하는 방법은 셋이다.
#   1. 기본 주소 그대로 프록시로 나간다(HTTPS_PROXY 를 빌드 인자로 준다).
#   2. 사내 미러에 같은 <버전>/<파일> 배치로 올려 두고 CORRETTO_BASE_URL 에 그 주소를 준다. http 주소의 미러를 프록시로 거쳐야 하면
#      소문자 http_proxy 를 준다(curl 은 대문자 HTTP_PROXY 를 읽지 않는다). 프록시를 거치지 않을 미러는 NO_PROXY 에 넣는다.
#   3. HTTPS 를 검사하는 망에서 인증서 오류가 나면 CORRETTO_TLS_VERIFY=0 을 준다. 인증서는 확인하지 않지만 체크섬은 그대로 확인한다.
ARG CORRETTO_VERSION=21.0.13.1.2
ARG CORRETTO_BASE_URL=https://corretto.aws/downloads/resources
ARG CORRETTO_SHA256_AMD64=c365469853d9296728bb19e629225fef04b45e595e3467d8a0778450367381e0
ARG CORRETTO_SHA256_ARM64=d251fb09557ccf7992d36b810a96df859531821679d21d0ab1eddc2b8e8c1ba0
ARG CORRETTO_TLS_VERIFY=1

# Corretto 를 maven 보다 먼저 설치한다. Corretto 패키지가 java7-runtime-headless 를 제공하므로 maven 이 Debian 의 OpenJDK 를 끌어오지 않는다.
# 끝에서 openjdk 패키지가 설치되어 있거나 java -version 이 정한 빌드가 아니면 빌드를 멈춘다.
# /opt/java21 은 Corretto 의 설치 경로를 가리킨다(amd64, arm64 모두 같은 경로). JAVA_HOME 은 여기를 가리킨다.
RUN if [ -n "$APT_MIRROR" ]; then sed -i "s|http://deb.debian.org|${APT_MIRROR%/}|g" /etc/apt/sources.list.d/debian.sources; fi \
    && apt-get update && apt-get install -y --no-install-recommends ca-certificates curl \
    && arch="$(dpkg --print-architecture)" \
    && case "$arch" in \
        amd64) sha_arg=CORRETTO_SHA256_AMD64; sha_want="$CORRETTO_SHA256_AMD64" ;; \
        arm64) sha_arg=CORRETTO_SHA256_ARM64; sha_want="$CORRETTO_SHA256_ARM64" ;; \
        *) echo "ERROR: unsupported architecture '$arch' for Amazon Corretto (amd64 and arm64 only); build with --platform linux/amd64 or linux/arm64." >&2; exit 1 ;; \
       esac \
    && deb="java-21-amazon-corretto-jdk_${CORRETTO_VERSION}-1_${arch}.deb" \
    && tls_opt="" && if [ "$CORRETTO_TLS_VERIFY" = "0" ]; then tls_opt="--insecure"; fi \
    && curl -fSL --no-progress-meter --retry 3 $tls_opt -o "/tmp/$deb" "${CORRETTO_BASE_URL%/}/${CORRETTO_VERSION}/$deb" \
    && sha_got="$(sha256sum "/tmp/$deb" | cut -d ' ' -f 1)" \
    && if [ "$sha_got" != "$sha_want" ]; then echo "ERROR: SHA-256 mismatch for $deb: expected $sha_want, got $sha_got. Check $sha_arg (and CORRETTO_VERSION / CORRETTO_BASE_URL)." >&2; exit 1; fi \
    && apt-get install -y --no-install-recommends "/tmp/$deb" \
    && rm -f "/tmp/$deb" \
    && apt-get install -y --no-install-recommends maven git unzip \
    && openjdk_pkgs="$(dpkg-query -W -f '${db:Status-Status} ${Package}\n' 'openjdk-*' 2>/dev/null | awk '$1 == "installed" { print $2 }')" \
    && if [ -n "$openjdk_pkgs" ]; then echo "ERROR: a Debian OpenJDK package was installed: $(echo $openjdk_pkgs). The image must carry Amazon Corretto only; check what depends on it (apt-cache rdepends) and CORRETTO_VERSION." >&2; exit 1; fi \
    && java_version="$(java -version 2>&1)" \
    && if ! printf '%s\n' "$java_version" | grep -qF "$CORRETTO_VERSION"; then echo "ERROR: java -version does not show Corretto $CORRETTO_VERSION: $(printf '%s' "$java_version" | tr '\n' ' '). Check CORRETTO_VERSION." >&2; exit 1; fi \
    && rm -rf /var/lib/apt/lists/* \
    && test -x /usr/lib/jvm/java-21-amazon-corretto/bin/javac \
    && ln -s /usr/lib/jvm/java-21-amazon-corretto /opt/java21

# sbmig 은 JAVA21_HOME / JAVA_HOME 을 읽는다 (설정 build.java_home 이 있으면 그것이 우선)
ENV JAVA_HOME=/opt/java21 \
    JAVA21_HOME=/opt/java21 \
    PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app \
    HOME=/home/sbmig

# UID 1000: 데이터 볼륨의 파일 소유자를 모든 컨테이너에서 같게
RUN useradd --create-home --uid 1000 sbmig && mkdir -p /app /data /workspace && chown -R sbmig:sbmig /app /data /workspace

# 의존성 먼저(캐시 레이어); 목록은 pyproject.toml 그 자체. PIP_TRUSTED_HOST 는 빈칸으로 나눈 호스트마다 --trusted-host 하나씩
COPY ${APP_DIR}/pyproject.toml ${DEPLOY_DIR}/docker/pip-deps.py /tmp/
RUN python /tmp/pip-deps.py /tmp/pyproject.toml \
    | xargs pip install ${PIP_INDEX_URL:+--index-url "$PIP_INDEX_URL"} \
        $(for h in $PIP_TRUSTED_HOST; do printf -- '--trusted-host %s ' "$h"; done) \
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

# 상태는 볼륨에, 이미지 안에는 두지 않는다: 실행 디렉터리(--out), KB(회사 yml 의 kb.dir), LLM 기록, Maven 로컬 저장소(/data/.m2)
# /data = 웹 UI 에이전트(Deployment), /workspace = 요청별 Job(emptyDir)
VOLUME ["/data"]
EXPOSE 8765

# `sbmig serve` 가 곧 uvicorn 이다(프로세스 안에서 uvicorn.run; 워커 하나 — 대기 줄이 프로세스 안에 있다. Job 모드에서 동시에 도는
# 빌드 파드 수는 SBMIG_JOB_MAX_PARALLEL 이 정한다).
# 루프백이 아닌 --host 는 SBMIG_WEB_TOKEN(Secret) 또는 SBMIG_WEB_OPEN=1(사내망 전용 페이지 — Deployment 의 env, 2026-11-05 결정)
# 둘 중 하나가 있어야 뜬다. 이미지에는 둘 다 없다. --graceful-timeout 은 종료 때 SSE 스트림을 닫는 여유.
# 요청별 Job 모델은 이 CMD 대신 /app/deploy/docker/run-job.sh 를 command 로 쓴다.
# 업로드 상한은 명령줄에 두지 않는다: --max-upload-mb 가 환경 변수보다 우선이라 ConfigMap 의 SBMIG_MAX_UPLOAD_MB 가 먹지 않았다.
# 이미지 기본값은 아래 ENV 이고 파드의 env(ConfigMap sbmig-env)가 그 값을 덮어쓴다.
ENV SBMIG_MAX_UPLOAD_MB=1024
CMD ["python", "-m", "sbmig.cli", "serve", "--host", "0.0.0.0", "--port", "8765", "--out", "/data/out", \
     "--graceful-timeout", "30"]
