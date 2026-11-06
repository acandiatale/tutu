#!/usr/bin/env bash
# sbmig 이미지를 만들어 tar 로 저장한다 — 이미지 레지스트리가 없는 클러스터용(deploy/README.md §1.1). 인프라팀이 이 tar 를 파드가 뜰 수 있는
# 노드마다 적재하고, 매니페스트는 `sbmig:<tag>` + imagePullPolicy Never 로 그 이미지를 쓴다.
#   사용:  deploy/docker/save-image.sh <tag> [docker build 추가 인자…]     (sbmig 체크아웃 루트에서; 회사 레포는 src/migration-agent/ 에서)
#          예) deploy/docker/save-image.sh 2026-11-06 --build-arg PIP_INDEX_URL=https://nexus.company/repository/pypi/simple
#   결과:  out/images/sbmig-<tag>.tar 와 sbmig-<tag>.tar.sha256 (SBMIG_IMAGE_OUT 으로 폴더 변경), 그리고 노드에서 칠 적재 명령.
#   항상 linux/amd64 로 만든다 — 노드가 amd64 다(Apple silicon / arm64 PC 에서 만든 arm64 이미지는 노드에서 exec format error).
#   buildx 가 있으면 --provenance=false --sbom=false: attestation 이 붙은 인덱스는 `ctr images import` 가 거부할 수 있다.
#   태그는 tar 마다 새로(latest 금지): imagePullPolicy Never 는 노드에 있는 그 태그를 그대로 쓰므로 같은 태그를 다시 적재해도 도는 파드는 옛 이미지다.
set -euo pipefail
TAG="${1:?사용: $0 <tag> [docker build 인자…]  — 태그는 tar 마다 새로(latest 금지)}"
shift
case "$TAG" in latest|LATEST) echo "ERROR: 'latest' 태그는 쓰지 않는다(README §1.1 — imagePullPolicy Never 와 함께 옛 이미지가 그대로 돈다)" >&2; exit 2;; esac
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
OUT="${SBMIG_IMAGE_OUT:-$ROOT/out/images}"
IMG="sbmig:$TAG"
mkdir -p "$OUT"
PROV=()
docker buildx version >/dev/null 2>&1 && PROV=(--provenance=false --sbom=false)
echo "build: $IMG (linux/amd64) from $ROOT"
docker build --platform linux/amd64 ${PROV[@]+"${PROV[@]}"} -f "$ROOT/deploy/docker/Dockerfile" -t "$IMG" "$@" "$ROOT"
docker run --rm --platform linux/amd64 "$IMG" python -m sbmig.cli --help >/dev/null && echo "check: python -m sbmig.cli --help ok"
echo "save: $OUT/sbmig-$TAG.tar"
docker save -o "$OUT/sbmig-$TAG.tar" "$IMG"
( cd "$OUT" && { command -v sha256sum >/dev/null 2>&1 && sha256sum "sbmig-$TAG.tar" || shasum -a 256 "sbmig-$TAG.tar"; } > "sbmig-$TAG.tar.sha256" )
ls -lh "$OUT/sbmig-$TAG.tar" | awk '{print "size:", $5}'
cat "$OUT/sbmig-$TAG.tar.sha256"
cat <<EOF

인프라팀이 노드마다(웹 파드와 Job 파드가 뜰 수 있는 모든 노드) — 런타임에 맞는 한 줄:
  containerd : sudo ctr -n k8s.io images import sbmig-$TAG.tar     확인: sudo crictl images | grep sbmig
  k3s / RKE2 : sudo k3s ctr images import sbmig-$TAG.tar
  docker     : docker load -i sbmig-$TAG.tar
그 다음 deploy/k8s/40-deployment.yaml 과 80-/81-*.template.yaml 의 image: 를 $IMG 로 (imagePullPolicy: Never 그대로) → kubectl apply.
EOF
