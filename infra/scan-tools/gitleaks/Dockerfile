# gitleaks Lambda 컨테이너 이미지 (compute=lambda). secret 스캔 단일 도구.
FROM public.ecr.aws/lambda/python:3.12

ARG GITLEAKS_VERSION=8.30.1
RUN dnf install -y tar gzip && \
    curl -sSL "https://github.com/gitleaks/gitleaks/releases/download/v${GITLEAKS_VERSION}/gitleaks_${GITLEAKS_VERSION}_linux_x64.tar.gz" \
      | tar -xz -C /usr/local/bin gitleaks && \
    dnf clean all

# shared normalize + handler. (빌드 컨텍스트는 infra/scan-tools/ 로 잡아 shared를 COPY.)
COPY shared/normalize.py ${LAMBDA_TASK_ROOT}/normalize.py
COPY gitleaks/handler.py ${LAMBDA_TASK_ROOT}/handler.py

CMD ["handler.handler"]
