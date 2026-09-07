#!/usr/bin/env bash
# Agora 백엔드 로컬 실행 — 실 AWS 모드 (registry=AwsRegistryAdapter, source=S3DynamoSourceStore,
# scanner=stepfn, poller off). AWS 크리덴셜로 실 리소스에 붙어요.
#
# 폴러는 로컬에서 켜지 않아요(공유 자원 경쟁 방지) — 호스팅된 포털의 폴러와 같은 배포 job 을 두고 경쟁해요.
#
# 사용:
#   ./api/run-backend.sh            # 포그라운드 실행 (Ctrl+C로 종료)
#   PORT=9101 ./api/run-backend.sh  # 포트 바꿔 실행
#
# 환경:
#   - api/.env  : AGORA_* 전부 (SCANNER=stepfn, SFN_ARN, POLLER, DEPLOY_*, COGNITO_* 등)
#   - AWS_PROFILE=dev / AWS_REGION=us-west-2 : .env에 없어 여기서 export
set -euo pipefail

# 이 스크립트 위치 기준 api/ 디렉토리로 이동 (어디서 실행하든 동작)
cd "$(dirname "$0")"

PORT="${PORT:-9100}"

# 1) 의존성 동기화 (dev 툴 + aws extra). 이미 최신이면 빠르게 통과.
uv sync --extra dev --extra aws

# 2) .env 로드 (set -a: 이후 정의되는 변수를 전부 export)
#    ⚠️ 호출자가 준 값이 .env를 이겨요. `source .env`만 하면 .env가 무조건 덮어써서
#    `AGORA_POLLER_ENABLED=0 ./api/run-backend.sh` 같은 일회성 override가 먹지 않고
#    role 가드(server.py:_poller_enabled)에 걸려 기동 자체가 막혀요.
_pre_env="$(mktemp)"
export -p | grep -E '^(declare -x |export )(AGORA_|AWS_|PORT=|RELOAD=)' > "$_pre_env" || true
set -a
# shellcheck disable=SC1091
source .env
set +a
# shellcheck disable=SC1090
source "$_pre_env"
rm -f "$_pre_env"

# 3) .env에 없는 AWS 세팅 (실 AWS 붙기 위해 필수)
export AWS_PROFILE="${AWS_PROFILE:-dev}"
export AWS_REGION="${AWS_REGION:-us-west-2}"

# 로컬도 배포서버와 동일하게 공유 DynamoDB 스토어를 써요(로컬 JSON 제거). .env에
# 값이 없으면 여기서 기본 테이블명을 채워 gov/bundle/connection이 항상 Dynamo로 배선되게 해요.
# ⚠️ 로컬 실행이 호스팅된 포털과 같은 테이블을 공유해요 — 파괴적 조작 주의.
export AGORA_GOV_TABLE="${AGORA_GOV_TABLE:-AgoraGov-dev}"
export AGORA_BUNDLE_TABLE="${AGORA_BUNDLE_TABLE:-AgoraBundle-dev}"
export AGORA_CONNECTION_TABLE="${AGORA_CONNECTION_TABLE:-AgoraConnection-dev}"
# AGORA_STAGE 와 별개인 실행환경 지문. .env가 명시하면 그 값을 존중해요.
export AGORA_ROLE="${AGORA_ROLE:-local}"

# 3-1) role 가드 선점검. server.py가 어차피 막지만, 여기서 원인·해법을 먼저 알려줘요.
if [ "${AGORA_ROLE}" = "local" ] \
  && [[ "${AGORA_POLLER_ENABLED:-0}" =~ ^(1|true|yes|TRUE|YES)$ ]] \
  && [[ ! "${AGORA_POLLER_FORCE:-0}" =~ ^(1|true|yes|TRUE|YES)$ ]]; then
  echo "✖ AGORA_ROLE=local 인데 AGORA_POLLER_ENABLED=${AGORA_POLLER_ENABLED} 예요." >&2
  echo "  로컬 폴러는 호스팅된 포털과 같은 공유 자원을 동시에 전진시켜요(공유 자원 경쟁 방지)." >&2
  echo "  → api/.env 에서 AGORA_POLLER_ENABLED=0 으로 두거나," >&2
  echo "    꼭 필요한 진단이면 AGORA_POLLER_FORCE=1 을 명시해 주세요." >&2
  exit 1
fi

echo "── Agora 백엔드 (실 AWS) ────────────────────────────"
echo "  PORT=$PORT  AWS_PROFILE=$AWS_PROFILE"
echo "  ROLE=$AGORA_ROLE  SCANNER=${AGORA_SCANNER:-?}  POLLER=${AGORA_POLLER_ENABLED:-?}"
echo "  REGISTRY_ID=${AGORA_REGISTRY_ID:-?}  DEPLOY_REGION=${AGORA_DEPLOY_REGION:-?}"
echo "─────────────────────────────────────────────────────"

# 4) uvicorn 기동. 개발 중 코드 반영을 원하면 --reload 추가 (실 AWS라 재시작 비용은 낮아요).
#    RELOAD=1 ./api/run-backend.sh 로 켤 수 있어요.
RELOAD_FLAG=""
if [ "${RELOAD:-0}" = "1" ]; then
  RELOAD_FLAG="--reload"
fi

exec .venv/bin/uvicorn agora.server:app --port "$PORT" $RELOAD_FLAG
