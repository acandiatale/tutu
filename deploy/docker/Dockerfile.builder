# OPTIONAL — the "builder" image for the split layout (deploy/README.md §3): JDK 21 + Maven + git, no Python, no app code.
# The agent container runs mvn here through `kubectl exec` (deploy/docker/mvn-k8s as build.mvn). Prefer the single image
# (deploy/docker/Dockerfile) unless project builds must be isolated from the container that holds the LLM credentials.
FROM debian:trixie-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        openjdk-21-jdk-headless maven git ca-certificates unzip \
    && rm -rf /var/lib/apt/lists/* \
    # /opt/java21 → the JDK of this architecture (amd64 / arm64 paths differ); JAVA_HOME points there
    && ln -s "$(dirname "$(dirname "$(readlink -f "$(command -v javac)")")")" /opt/java21

ENV JAVA_HOME=/opt/java21 \
    JAVA21_HOME=/opt/java21 \
    HOME=/home/sbmig

# the same uid as the agent image: both write the shared /data volume
RUN useradd --create-home --uid 1000 sbmig && mkdir -p /data && chown sbmig:sbmig /data
USER sbmig
WORKDIR /data

# stays alive; the agent execs `mvn` into it and the pod (or the sidecar) goes away with the run
CMD ["sleep", "infinity"]
