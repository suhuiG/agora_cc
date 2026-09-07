# trivy Lambda 컨테이너 이미지 (compute=lambda). sbom_cve 스캔 단일 도구.
# air-gap: 취약점 DB를 빌드 시 번들(download-db-only) → 런타임 --skip-db-update로 완전 offline.
FROM public.ecr.aws/lambda/python:3.12

ARG TRIVY_VERSION=0.72.0
RUN dnf install -y tar gzip && \
    curl -sSL "https://github.com/aquasecurity/trivy/releases/download/v${TRIVY_VERSION}/trivy_${TRIVY_VERSION}_Linux-64bit.tar.gz" \
      | tar -xz -C /usr/local/bin trivy && \
    dnf clean all

# 취약점 DB를 빌드 시점에 /opt/trivy-cache 로 pre-download(번들). 런타임엔 인터넷이 없으므로
# 여기서 받아둔 DB만 씀. Lambda 파일시스템은 읽기전용이라 런타임에 /tmp로 복사해 사용.
ENV TRIVY_CACHE_DIR=/opt/trivy-cache
RUN trivy --cache-dir /opt/trivy-cache image --download-db-only && \
    trivy --cache-dir /opt/trivy-cache image --download-java-db-only

# shared normalize + handler. (빌드 컨텍스트는 infra/scan-tools/ 로 잡아 shared를 COPY.)
COPY shared/normalize.py ${LAMBDA_TASK_ROOT}/normalize.py
COPY trivy/handler.py ${LAMBDA_TASK_ROOT}/handler.py

CMD ["handler.handler"]
