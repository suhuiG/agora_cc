# Agora 웹(Next.js 16 SSR + BFF) — App Runner 컨테이너 이미지.
# output:'standalone' 산출물을 그대로 실행해요(Amplify 미사용 → Next 버전 제약 없음).
# 실 AWS 좌표/Cognito 설정은 굽지 않고 App Runner 런타임 env로 주입해요.
# 세션 DynamoDB 쓰기는 App Runner 인스턴스 역할(SigV4)로 처리해요(자격증명 env 불필요).

# ── build ──────────────────────────────────────────────────────────────
FROM node:22-bookworm-slim AS builder
WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci

COPY . .

# 빌드 중 getAuthConfig()가 평가돼도 터지지 않게 유효한 플레이스홀더를 줘요.
# NEXT_PUBLIC_*가 아니라 클라이언트 번들에 새지 않고, 런타임 값은 App Runner env가 덮어써요.
ENV NEXT_TELEMETRY_DISABLED=1 \
    AGORA_WEB_AUTH_MODE=cognito \
    AGORA_WEB_BASE_URL=https://placeholder.invalid \
    AGORA_API_URL=https://placeholder.invalid \
    AGORA_AUTH_COGNITO_DOMAIN=https://placeholder.invalid \
    AGORA_AUTH_COGNITO_CLIENT_ID=build-placeholder \
    AGORA_AUTH_COGNITO_USER_POOL_ID=build-placeholder \
    AGORA_AUTH_SESSION_TABLE=build-placeholder \
    AGORA_AUTH_REGION=ap-northeast-2
RUN npm run build

# ── runtime ────────────────────────────────────────────────────────────
FROM node:22-bookworm-slim AS runner
WORKDIR /app
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    HOSTNAME=0.0.0.0

# standalone 산출물 + 정적 자원 + public.
COPY --from=builder /app/.next/standalone ./
COPY --from=builder /app/.next/static ./.next/static
COPY --from=builder /app/public ./public

EXPOSE 3000
CMD ["node", "server.js"]
