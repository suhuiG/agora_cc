# QUICKSTART

코드를 읽고 검증하고 합성(`cdk synth`)까지의 절차. 배포(`cdk deploy`)는 미포함. 어느 계정에
무엇을 만들지는 읽는 쪽의 판단 영역.

## 준비물

| 도구 | 버전 | 쓰는 곳 |
| --- | --- | --- |
| Python | 3.12 | `api/` |
| [uv](https://docs.astral.sh/uv/) | 최신 | `api/` 의존성·실행 |
| Node.js | 20 이상 | `web/`, `infra/` |
| AWS CDK CLI | v2 | `infra/` |
| Docker | 데몬 실행 중 | 포털 스택 합성·배포에만 필요 |

AWS 크리덴셜은 읽기 권한으로 합성까지 가능. 리전은 `ap-northeast-2`(서울) 기준이고, AWS Agent
Registry 만 `us-east-1` 사용.

## 1. 의존성 설치

```bash
cd api  && uv sync --extra dev --extra aws
cd ../web   && npm ci
cd ../infra && npm ci
```

## 2. 검증

```bash
cd api   && uv run ruff check .
cd ../web   && npm run lint && npm run build
cd ../infra && npm run build
```

## 3. CDK 합성

CDK 앱은 좌표 누락 시 합성 자체를 거부. 소스에 계정·풀 ID·Gateway ID 가 없기 때문. 합성만
확인할 때는 형식이 맞는 더미 값 사용 가능.

```bash
cd infra

export CDK_DEFAULT_ACCOUNT=<12자리 계정번호>
export CDK_DEFAULT_REGION=ap-northeast-2

# 사람 포털 origin. prod 스테이지는 loopback 거부.
export AGORA_WEB_BASE_URL=https://example.cloudfront.net

# AgoraIdentity 의 HumanCognitoClientId output
export AGORA_GATEWAY_HUMAN_CLIENT_IDS=<client id>

# Gateway 인바운드 발급자. 둘은 같은 풀을 가리켜야 함.
export AGORA_M2_OAUTH_COGNITO_USER_POOL_ID=ap-northeast-2_xxxxxxxxx
export AGORA_M2_OAUTH_DISCOVERY_URL=https://cognito-idp.ap-northeast-2.amazonaws.com/ap-northeast-2_xxxxxxxxx/.well-known/openid-configuration

# Runtime 인가 Lambda 가 조회할 Registry 좌표
export AGORA_REGISTRY_ID=<registry id>
export AGORA_DEPLOY_COGNITO_DISCOVERY_URL=$AGORA_M2_OAUTH_DISCOVERY_URL
export AGORA_DEPLOY_COGNITO_CLIENT_ID=<workload client id>
export AGORA_DEPLOY_COGNITO_SCOPE=https://agora-m2-oauth-<stage>/invoke

npx cdk list -c stage=dev
npx cdk synth -c stage=dev
```

스테이지는 CDK 컨텍스트로 전달(`-c stage=dev`). 기본값은 안전 우선으로 `prod`.

포털 스택은 별도 플래그. 활성화 시 Docker 데몬 필요.

```bash
npx cdk synth -c stage=dev -c portal=true
```

선택 스택 둘은 각각 `-c monitoringAggregate=true`, `-c telemetryArchive=true` 로 활성화.

### 합성이 크리덴셜을 요구할 때

`Need to perform AWS calls for account ...` 는 가용영역 조회 때문. 유효한 크리덴셜로 한 번
합성하면 `cdk.context.json` 에 캐시. 이 캐시는 계정번호가 키에 들어가므로 커밋 대상이 아님
(`.gitignore` 처리).

## 4. 로컬 실행

백엔드와 웹 양쪽 프로세스에 AWS 크리덴셜 필요. 웹의 BFF 가 세션을 DynamoDB 에 기록하기 때문.
크리덴셜 없이 웹만 기동하면 로그인이 `401` 로 실패하는데 증상은 비밀번호 오류처럼 보임. 실제
원인은 세션 테이블 쓰기 실패.

```bash
cp api/.env.example api/.env       # 값 채우기
cp web/.env.example web/.env.local # 값 채우기

bash api/run-backend.sh   # 포트 9100
bash web/run-web.sh       # 포트 3000
```

웹은 3000 번 포트로 기동. `AGORA_WEB_BASE_URL` 과 Cognito 콜백이 그 포트 기준.

로컬 백엔드의 배포 폴러는 기본 비활성. 호스팅된 포털과 같은 배포 job 을 두고 경쟁하기 때문.
로컬에서 배포 경로 시험이 필요하면 폴러를 켜지 말고 jobs 테이블만 분리
(`AGORA_DEPLOY_JOBS_TABLE=<임시 테이블>`). registry 와 identity 원장은 공유한 채로 job 만 격리.

## 5. 배포 순서 (참고)

스택 간 참조로 인한 순서.

```
AgoraCatalogStorage → AgoraGovernanceScan → AgoraGovernanceScanTools
                    → AgoraRuntimeDeploy → AgoraIdentity
                    → AgoraM2OAuthGateway → AgoraRuntimeAuthorization
                    → AgoraPortal (-c portal=true)
```

배포 전 확인 항목:

- 합성된 `AWS::Lambda::Permission` 에 `Principal: "*"` 부재. Runtime 인가 Function URL 은
  `AuthType=AWS_IAM` 유지.
- Gateway policy engine 모드가 `ENFORCE` 인지 확인. 플래그 미전달 시 `ENFORCE` 가 기본이지만,
  `cdk diff` 에 `ENFORCE → LOG_ONLY` 혼입 여부는 육안 확인.
- 스캔 도구 중 semgrep 은 Fargate 라 이미지를 ECR 에 별도 push
  (`infra/scan-tools/build-and-push.sh`). CDK 는 이 이미지를 재빌드하지 않음.
- `AgoraGovernanceScanTools` 는 VPC 조회를 수행하므로 합성에 크리덴셜 필요.

## 환경변수 계약

두 부류.

`[S]` 는 공유 데이터·인프라 좌표. 로컬과 호스팅 포털이 같은 리소스를 가리키게 하는 값이고 CDK
output 에서 전달.

`[E]` 는 환경별 운영 정책. 프로세스별 소유 값이라 포털 배포로 복사되지 않고 `AgoraPortal`
스택이 명시적으로 주입.

`api/.env.example` 이 부류 표시와 함께 전체 목록 보유. 백엔드 변수는 `AGORA_*`, 브라우저 노출
변수는 `NEXT_PUBLIC_*` 접두어. 크리덴셜 원문은 Secrets Manager 또는 SSM 에 두고 소스·예시
파일에 미포함.
