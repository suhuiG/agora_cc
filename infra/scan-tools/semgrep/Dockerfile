# semgrep Fargate 이미지 (compute=fargate). sast 스캔 단일 도구. 오프라인 룰 번들(air-gap).
FROM --platform=linux/amd64 python:3.12-slim

ARG SEMGREP_VERSION=1.16.0
ARG SEMGREP_RULES_REF=develop
RUN apt-get update && apt-get install -y --no-install-recommends git \
 && git clone --depth 1 --branch "${SEMGREP_RULES_REF}" --filter=blob:none --sparse \
      https://github.com/semgrep/semgrep-rules.git /opt/semgrep-rules \
 && git -C /opt/semgrep-rules sparse-checkout set python javascript typescript generic \
 && rm -rf /opt/semgrep-rules/.git \
 && apt-get purge -y git && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir boto3 "semgrep==${SEMGREP_VERSION}"

ENV SEMGREP_RULES_DIR=/opt/semgrep-rules
WORKDIR /app
# 빌드 컨텍스트 infra/scan-tools/ — shared + semgrep 둘 다 COPY.
COPY shared/normalize.py semgrep/entrypoint.py /app/
ENTRYPOINT ["python", "entrypoint.py"]
