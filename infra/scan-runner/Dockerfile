# scan-runner (linux/amd64, Fargate x86). gitleaks(secret) + semgrep(sast).
FROM --platform=linux/amd64 python:3.12-slim

ARG GITLEAKS_VERSION=8.30.1
ARG SEMGREP_VERSION=1.16.0
# semgrep 공식 룰 커밋 pin — air-gap에서도 재현 가능한 룰셋(빌드 시 1회, 미러 계층 §1.2).
ARG SEMGREP_RULES_REF=develop
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates git \
 && curl -sSL "https://github.com/gitleaks/gitleaks/releases/download/v${GITLEAKS_VERSION}/gitleaks_${GITLEAKS_VERSION}_linux_x64.tar.gz" \
    | tar -xz -C /usr/local/bin gitleaks \
 # semgrep 공식 룰을 이미지에 번들(오프라인 SAST). sparse checkout으로 언어 룰만 받아요.
 && git clone --depth 1 --branch "${SEMGREP_RULES_REF}" --filter=blob:none --sparse \
      https://github.com/semgrep/semgrep-rules.git /opt/semgrep-rules \
 && git -C /opt/semgrep-rules sparse-checkout set python javascript typescript generic \
 && rm -rf /opt/semgrep-rules/.git \
 && apt-get purge -y curl git && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

# boto3(S3 I/O) + semgrep(SAST). semgrep은 pip 패키지(바이너리 포함).
RUN pip install --no-cache-dir boto3 "semgrep==${SEMGREP_VERSION}"

# 번들된 룰 경로 — entrypoint가 --config로 참조. air-gap에서 인터넷 없이 SAST.
ENV SEMGREP_RULES_DIR=/opt/semgrep-rules

WORKDIR /app
COPY normalize.py entrypoint.py /app/

ENTRYPOINT ["python", "entrypoint.py"]
