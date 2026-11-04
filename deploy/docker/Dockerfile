# sbmig — ONE image: Python 3.11 + JDK 21 + Maven + git. The agent runs mvn / OpenRewrite itself, exactly as on the
# operator's PC today (the build-fix loop calls mvn after every attempt — a local Maven is the simplest and fastest way).
# Build from the sbmig checkout root:   docker build -f deploy/docker/Dockerfile -t sbmig:<tag> .
# (company layout: the checkout is src/migration-agent/ of a bigger repo → run the build from THAT directory, or
#  change the two COPY lines to `COPY src/migration-agent/… …`)
ARG PYTHON_IMAGE=python:3.11-slim-trixie
FROM ${PYTHON_IMAGE}

# in-house mirrors (optional): --build-arg PIP_INDEX_URL=https://nexus.example/repository/pypi/simple
#                              --build-arg PIP_TRUSTED_HOST=nexus.example
ARG PIP_INDEX_URL=
ARG PIP_TRUSTED_HOST=

RUN apt-get update && apt-get install -y --no-install-recommends \
        openjdk-21-jdk-headless maven git ca-certificates unzip \
    && rm -rf /var/lib/apt/lists/* \
    # /opt/java21 → the JDK of this architecture (amd64 / arm64 paths differ); JAVA_HOME points there
    && ln -s "$(dirname "$(dirname "$(readlink -f "$(command -v javac)")")")" /opt/java21

# sbmig reads JAVA21_HOME / JAVA_HOME (config build.java_home may override them)
ENV JAVA_HOME=/opt/java21 \
    JAVA21_HOME=/opt/java21 \
    PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app \
    HOME=/home/sbmig

# UID 1000: the data volume's files are owned by the same uid in every container that mounts it
RUN useradd --create-home --uid 1000 sbmig && mkdir -p /app /data && chown -R sbmig:sbmig /app /data

# dependencies first (a cached layer); the list is pyproject.toml's own
COPY pyproject.toml deploy/docker/pip-deps.py /tmp/
RUN python /tmp/pip-deps.py /tmp/pyproject.toml \
    | xargs pip install ${PIP_INDEX_URL:+--index-url "$PIP_INDEX_URL"} ${PIP_TRUSTED_HOST:+--trusted-host "$PIP_TRUSTED_HOST"} \
    && rm -f /tmp/pyproject.toml /tmp/pip-deps.py

# the application: config/, knowledge/ and sbmig/ must sit together (config.py takes the repo root from its own path)
COPY --chown=sbmig:sbmig . /app/
WORKDIR /app
USER sbmig

# state lives on the volume, never in the image: runs (--out), the KB (kb.dir in the company yml), LLM records
VOLUME ["/data", "/home/sbmig/.m2"]
EXPOSE 8765

# `sbmig serve` IS uvicorn (uvicorn.run in-process, one worker on purpose: one migration at a time, in-process queue);
# a non-loopback --host requires SBMIG_WEB_TOKEN (the Secret). --graceful-timeout lets SSE streams close on shutdown.
CMD ["python", "-m", "sbmig.cli", "serve", "--host", "0.0.0.0", "--port", "8765", "--out", "/data/out", \
     "--graceful-timeout", "30", "--max-upload-mb", "1024"]
