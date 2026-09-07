# QUICKSTART

코드를 읽고 검증하고 합성(`cdk synth`)까지 하는 절차예요. 배포(`cdk deploy`)는 넣지 않았어요.
어느 계정에 무엇을 만들지는 읽는 분이 결정할 일이에요.

## 준비물

| 도구 | 버전 | 쓰는 곳 |
| --- | --- | --- |
| Python | 3.12 | `api/` |
| [uv](https://docs.astral.sh/uv/) | 최신 | `api/` 의존성·실행 |
| Node.js | 20 이상 | `web/`, `infra/` |
| AWS CDK CLI | v2 | `infra/` |
| Docker | 데몬 실행 중 | 포털 스택 합성·배포에만 필요 |

AWS 크리덴셜은 읽기 권한이면 합성까지 돼요. 리전은 `ap-northeast-2`(서울)를 기준으로
작성돼 있고, AWS Agent Registry 만 `us-east-1` 을 써요.

## 1. 의존성 설치

```bash
cd api  && uv sync --extra dev --extra aws
cd ../web   && npm ci
cd ../infra && npm ci
```

## 2. 검증

이 저장소에는 테스트 스위트가 없어요. 사본을 만들 때 전부 통과한 상태로 잘라냈어요. 남은
검증은 정적 검사, 빌드, CDK 합성이에요.

```bash
cd api   && uv run ruff check .
cd ../web   && npm run lint && npm run build
cd ../infra && npm run build
```

## 3. CDK 합성

CDK 앱은 좌표가 없으면 합성 자체를 거부해요. 소스에 계정, 풀 ID, Gateway ID 가 하나도 박혀
있지 않기 때문이에요. 합성만 해 볼 때는 형식이 맞는 더미 값으로 채워도 돼요.

```bash
cd infra

export CDK_DEFAULT_ACCOUNT=<12자리 계정번호>
export CDK_DEFAULT_REGION=ap-northeast-2

# 사람 포털 origin. prod 스테이지에서는 loopback 을 거부해요.
export AGORA_WEB_BASE_URL=https://example.cloudfront.net

# AgoraIdentity 의 HumanCognitoClientId output
export AGORA_GATEWAY_HUMAN_CLIENT_IDS=<client id>

# Gateway 인바운드 발급자. 이 둘은 같은 풀을 가리켜야 해요.
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

스테이지는 CDK 컨텍스트로 넘겨요(`-c stage=dev`). 기본값은 안전 우선으로 `prod` 예요.

포털 스택은 별도 플래그가 있어요. 켜면 Docker 데몬이 필요해요.

```bash
npx cdk synth -c stage=dev -c portal=true
```

선택 스택 둘은 각각 `-c monitoringAggregate=true`, `-c telemetryArchive=true` 로 켜요.

### 합성이 크리덴셜을 요구할 때

`Need to perform AWS calls for account ...` 가 나오면 가용영역 조회 때문이에요. 유효한
크리덴셜로 한 번 합성하면 `cdk.context.json` 에 캐시돼요. 이 저장소에는 그 캐시 파일이
없어요. 원본 계정번호가 키에 들어가서 뺐어요.

## 4. 로컬 실행

백엔드와 웹 양쪽 프로세스에 AWS 크리덴셜이 필요해요. 웹의 BFF 가 세션을 DynamoDB 에 쓰기
때문이에요. 크리덴셜 없이 웹만 띄우면 로그인이 `401` 로 실패하는데, 비밀번호 오류처럼 보여요.
실제로는 세션 테이블 쓰기 실패예요.

```bash
cp api/.env.example api/.env       # 값 채우기
cp web/.env.example web/.env.local # 값 채우기

bash api/run-backend.sh   # 포트 9100
bash web/run-web.sh       # 포트 3000
```

웹은 3000 번 포트로 띄워요. `AGORA_WEB_BASE_URL` 과 Cognito 콜백이 그 포트를 기준으로 쓰여
있어요.

로컬 백엔드에서 배포 폴러는 기본으로 꺼져 있어요. 호스팅된 포털과 같은 배포 job 을 두고
경쟁하기 때문이에요. 로컬에서 배포 경로를 시험해야 하면 폴러를 켜지 말고 jobs 테이블만
분리하세요(`AGORA_DEPLOY_JOBS_TABLE=<임시 테이블>`). registry 와 identity 원장은 공유한 채로
job 만 격리돼요.

## 5. 배포 순서 (참고)

배포는 직접 판단해 주세요. 스택 간 참조 때문에 순서가 있어요.

```
AgoraCatalogStorage → AgoraGovernanceScan → AgoraGovernanceScanTools
                    → AgoraRuntimeDeploy → AgoraIdentity
                    → AgoraM2OAuthGateway → AgoraRuntimeAuthorization
                    → AgoraPortal (-c portal=true)
```

배포 전에 확인할 것:

- 합성된 `AWS::Lambda::Permission` 에 `Principal: "*"` 가 없어야 해요. Runtime 인가 Function
  URL 은 `AuthType=AWS_IAM` 을 유지해요.
- Gateway policy engine 모드가 `ENFORCE` 인지 확인해요. 플래그를 안 넘기면 `ENFORCE` 가
  기본이지만, `cdk diff` 에 `ENFORCE → LOG_ONLY` 가 섞여 들어오지 않는지 눈으로 보세요.
- 스캔 도구 컨테이너 중 semgrep 은 Fargate 라 이미지를 ECR 에 따로 올려야 해요
  (`infra/scan-tools/build-and-push.sh`). CDK 가 이 이미지를 다시 빌드하지 않아요.
- `AgoraGovernanceScanTools` 는 VPC 조회를 하므로 합성에 크리덴셜이 필요해요.

## 환경변수 계약

환경변수는 두 부류예요.

`[S]` 는 공유 데이터·인프라 좌표예요. 로컬과 호스팅 포털이 같은 리소스를 가리키게 하는 값이고,
CDK output 에서 옮겨 담아요.

`[E]` 는 환경별 운영 정책이에요. 프로세스마다 따로 소유하는 값이라 포털 배포로 복사되지 않고
`AgoraPortal` 스택이 명시적으로 주입해요.

`api/.env.example` 이 부류 표시와 함께 전체 목록을 담고 있어요. 백엔드 변수는 `AGORA_*`,
브라우저에 노출되는 변수는 `NEXT_PUBLIC_*` 접두어를 써요. 크리덴셜 원문은 Secrets Manager
또는 SSM 에 두고 소스나 예시 파일에 넣지 않아요.
