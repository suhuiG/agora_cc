#!/usr/bin/env bash
# 도구별 이미지 빌드 + ECR push. 사용: ./build-and-push.sh <toolId> <stage> <account> [region]
# 전제: Docker Desktop 조직 로그인(amazonians) 완료, aws cli 자격 구성.
set -euo pipefail
TOOL="${1:?toolId required}"; STAGE="${2:?stage required}"; ACCOUNT="${3:?account required}"
REGION="${4:-ap-northeast-2}"
REPO="agora-tool-${TOOL}-${STAGE}"
URI="${ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com/${REPO}"
CTX="$(cd "$(dirname "$0")" && pwd)"   # infra/scan-tools

aws ecr get-login-password --region "${REGION}" | docker login --username AWS --password-stdin "${ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com"
docker build --platform linux/amd64 -t "${URI}:latest" -f "${CTX}/${TOOL}/Dockerfile" "${CTX}"
docker push "${URI}:latest"
echo "pushed ${URI}:latest"
