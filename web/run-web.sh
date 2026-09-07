#!/usr/bin/env bash
# Agora 웹(프론트 + BFF) 로컬 실행 — 실 AWS 모드.
#
# ⚠️ 왜 AWS 자격증명이 필요한가:
#   BFF의 세션 저장소(web/src/lib/auth/session-store.ts)가 DynamoDB
#   (AGORA_AUTH_SESSION_TABLE, 기본 ap-northeast-2)에 opaque 세션을 써요.
#   AWS_PROFILE 없이 `npm run dev`만 하면 putSession이 자격증명 없음으로 실패해
#   로그인 시 POST /api/auth/session이 401을 뱉어요(백엔드 /api/me는 200인데도).
#   backend(run-backend.sh)는 creds를 export하지만 웹은 별도라 여기서 export해요.
#
# ⚠️ 포트: 콜백·AGORA_WEB_BASE_URL이 http://localhost:3000 기준이라 기본 3000으로 띄워요.
#   (인앱 SRP 로그인은 콜백을 안 쓰지만, cookie/redirect 계약을 위해 3000으로 통일.)
#
# 사용:
#   ./web/run-web.sh              # 포트 3000
#   PORT=3001 ./web/run-web.sh    # 포트 변경
set -euo pipefail
cd "$(dirname "$0")"

PORT="${PORT:-3000}"
# .env.local은 Next.js가 자동 로드해요. AWS creds/region만 여기서 보장해요.
export AWS_PROFILE="${AWS_PROFILE:-dev}"
export AWS_REGION="${AWS_REGION:-ap-northeast-2}"

echo "── Agora 웹 (실 AWS) ─────────────────────────────────"
echo "  PORT=$PORT  AWS_PROFILE=$AWS_PROFILE  AWS_REGION=$AWS_REGION"
echo "  세션 테이블 쓰기(DynamoDB)에 위 creds가 쓰여요."
echo "──────────────────────────────────────────────────────"

exec npm run dev -- -p "$PORT"
