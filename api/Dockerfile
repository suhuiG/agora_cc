# Agora 백엔드(FastAPI) — App Runner 컨테이너 이미지.
# 실 AWS 좌표(AGORA_*)는 굽지 않고 App Runner 런타임 env로 주입해요(.dockerignore가 .env 제외).
# 인증/권한은 앱 레벨(Cognito Bearer) + App Runner 인스턴스 역할(SigV4)로 처리해요.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# 1) 의존성 레이어(캐시) — 소스 없이 lock 기준으로 prod 의존성만(dev extra 제외, aws extra 포함).
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --extra aws --no-install-project

# 2) 앱 소스 + 프로젝트 설치.
COPY . .
RUN uv sync --frozen --no-dev --extra aws

# App Runner가 이 포트로 헬스체크(/api/health)·트래픽을 보내요.
EXPOSE 9100
CMD ["uvicorn", "agora.server:app", "--host", "0.0.0.0", "--port", "9100"]
