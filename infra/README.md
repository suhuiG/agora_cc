# Agora Infra (CDK)

Agora 의 AWS 리소스를 정의하는 CDK(TypeScript) 프로젝트. 리전은 `ap-northeast-2` 고정이고, AWS
Agent Registry 만 `us-east-1` 사용.

| 스택 | 담당 |
| --- | --- |
| `AgoraCatalogStorage-<stage>` | source S3, catalog DynamoDB, 공유 API 실행 롤 |
| `AgoraIdentity-<stage>` | 사람용 Cognito User Pool, `user`/`admin` 그룹, public web client, pre-token Lambda, Identity 원장 테이블 |
| `AgoraRuntimeDeploy-<stage>` | MCP·Agent 배포 파이프라인, CodeBuild, 실행 롤, permission boundary, workload Cognito M2M |
| `AgoraM2OAuthGateway-<stage>` | AgentCore Gateway, Cedar policy engine, REQUEST interceptor, 타깃 Lambda |
| `AgoraRuntimeAuthorization-<stage>` | Runtime 도구 호출 판정 API (Lambda Function URL) |
| `AgoraGovernanceScan-<stage>` | 스캔 Step Functions, 스캔 버킷 |
| `AgoraGovernanceScanTools-<stage>` | gitleaks, semgrep, trivy, LLM 심사 실행체 |
| `AgoraPortal-<stage>` | ECS Fargate + ALB + CloudFront 호스팅 (`-c portal=true`) |
| `AgoraTelemetryArchive-<stage>` | 원본 runtime 로그·공유 span 이관 (`-c telemetryArchive=true`) |
| `AgoraMonitoringAggregate-<stage>` | 호출 집계 테이블·ingest (`-c monitoringAggregate=true`) |

스테이지는 CDK 컨텍스트로 전달(`-c stage=dev`). 기본값은 안전 우선으로 `prod`.

## 준비

Docker 와 `uv` 선행 설치 필요. scan-tools 의 `DockerImageFunction` 은 Docker 를 쓰고, Python
asset bundling 은 로컬 `uv` 또는 Docker 를 사용. CDK bootstrap 은 스택이 배포되는 서울 리전 한
곳으로 충분. `us-east-1` bootstrap 은 불필요.

```bash
cd infra
npm ci
npm run build
npx cdk bootstrap aws://<account-id>/ap-northeast-2 -c stage=dev
```

`cdk bootstrap` 도 앱을 합성하므로 환경변수와 `-c stage` 가 필요. 기본 stage 인 `prod` 는
`webBaseUrl` 을 요구해 부트스트랩이 먼저 실패.

계정·리전 수준 선행조건(X-Ray trace destination, VPC 쿼터, 사람 사용자 2명)은 저장소 루트
[QUICKSTART.md](../QUICKSTART.md) "계정 선행조건" 절 참조. CDK 가 만들어 주지 않고, 빠지면
`AgoraM2OAuthGateway`·`AgoraMonitoringAggregate`·`AgoraPortal` 이 CREATE_FAILED.

합성에 필요한 환경변수 목록은 QUICKSTART 3절, CDK output → 환경변수 매핑은 5절 참조. 좌표
누락 시 앱이 합성 자체를 거부.

## Cognito 도메인 접두어

Cognito 도메인 접두어는 전역 유일. 그래서 `lib/cognito-domain.ts` 는 접두어를
`<base>-<stage>-<accountId>` 로 파생하고, 소스에는 어떤 실배포 접두어도 핀하지 않음. 기존 배포의
접두어 보존이 필요하면 주입:

```bash
npx cdk deploy AgoraIdentity-dev -c stage=dev \
  -c cognitoDomainPrefix:human:dev=<prefix>
```

`npm run check:cognito -- --stage <stage>` 는 합성값과 live Cognito 도메인을 전량 대조. 배포 전
1회 실행으로 접두어 불일치 선행 검출.

## Gateway 강제 모드와 interceptor

`AgoraM2OAuthGateway` 의 policy engine mode 와 interceptor 부착은 컨텍스트 값이고 둘 다 안전한
쪽이 기본값: `m2OAuthGatewayMode=ENFORCE`, `m2OAuthInterceptor=on`. 낮추거나 떼려면
`-c m2OAuthGatewayMode=LOG_ONLY` 또는 `-c m2OAuthInterceptor=off` 명시 필요.

기본값 근거: 컨텍스트 없는 배포가 라이브 `interceptorConfigurations` 를 `undefined` 로 덮어 유일한
강제 지점을 제거한 사례 존재. `--exclusively` 를 붙여도 앱 전체를 synth 하므로, 이 스택만
배포할 때도 환경변수 전량 필요.

## Runtime 인가 Function URL

Function URL 은 `AuthType=AWS_IAM` 이고 invoke permission 을 배포 계정 principal 로 제한.
`Principal: "*"` 는 금지. Agent Runtime 실행 롤만 stage 별 authorizer 함수에
`lambda:InvokeFunctionUrl` 과 `lambda:InvokeFunction` 가능하고, 생성된 Agent 는 요청을 SigV4 로
서명. Cognito M2M JWT 는 `X-Agora-Workload-Token` 으로 별도 전달해 애플리케이션에서도 검증. 외부
unsigned 요청은 Lambda edge 에서 `403`.

이 스택 배포에는 `AGORA_REGISTRY_ID` 필요. 출력 `RuntimeAuthorizationUrl` 을 API 의
`AGORA_RUNTIME_AUTHORIZATION_URL` 로 설정한 뒤 Agent 를 배포하면 그 URL 이 Runtime 환경에 주입.

## pre-token Lambda

Identity 스택의 `agora-perms-claim-<stage>` 는 Cognito access token 발급 직전에 두 가지 수행.

1. 사용자의 활성·미만료 권한을 `perms` claim 으로 인코딩해 주입.
2. Gateway 입장 scope(`<resource-server>/invoke`)를 `scopesToAdd` 로 적재. 값의 출처는
   `m2OAuthInvokeScope(stage)` 헬퍼이고, Gateway 의 `allowedScopes` 도 같은 함수를 쓰므로 두 값의
   드리프트 불가.

사용자 키는 email 이 아니라 Cognito `sub`. 함수 권한은 Identity 테이블의 `dynamodb:Query` 하나.
조회 실패나 claim 인코딩 실패 시 token 발급도 실패하는 fail-closed 경로.

## 첫 배포 순서

`cdk deploy --all` 은 스택 의존 순서를 자동 해석. 단 합성이 요구하는 값 중 셋이 배포 후에만
생겨서 한 바퀴 필요 — Cognito 좌표 둘과 Registry ID.

`AgoraCatalogStorage` 가 `AgoraM2OAuthGateway` 의 policy engine·봇 pool ARN 을 import 하므로
Gateway 가 먼저. 그런데 Gateway authorizer 의 `discoveryUrl` 은 실재하는 pool 이어야 생성이
통과하고, 그 pool(사람 pool)은 `AgoraIdentity` 에 있고 Identity 는 CatalogStorage 뒤. 그래서 첫
배포에는 임시 pool 하나가 필요.

Registry 는 백엔드가 미지정일 때만 조회/생성하는데(`shared/deps.py`), 미지정이면 합성이
실패하므로 SDK 로 먼저 만들어야 함.

전체 절차와 명령은 [QUICKSTART.md](../QUICKSTART.md) "신규 계정 첫 배포 — 2-pass 부트스트랩".

```bash
# pass 1 — 임시 pool + 미리 만든 registry ID 로 전체 배포
npx cdk deploy --all -c stage=dev

# pass 2 — 실제 Identity output 으로 좌표 교체하고 포털까지
npx cdk deploy --all -c stage=dev -c portal=true
```

`AGORA_DEPLOY_COGNITO_*` 를 처음 공급하는 배포는 consumer 를 먼저.
`AgoraRuntimeAuthorization` 이 그 값 없이 `AgoraRuntimeDeploy` export 를 import 하고 있어서,
producer 를 먼저 배포하면 `Cannot delete export … as it is in use by` 로 롤백.

```bash
npx cdk deploy AgoraRuntimeAuthorization-dev --exclusively -c stage=dev
```

RuntimeAuthorization 배포는 cross-stack dependency 때문에 Identity 도 함께 갱신 가능. 로컬 웹을
3000 이 아닌 포트로 기동한다면 그 배포에도 `AGORA_WEB_BASE_URL` 을 같은 포트로 지정. 누락 시
Cognito callback 이 기본 3000 으로 회귀.

## 스캔 도구 이미지

semgrep 은 Fargate 라 CDK 가 이미지를 재빌드하지 않음. `scan-tools/build-and-push.sh` 로 ECR 에
직접 push. 나머지 세 도구는 `cdk deploy` 가 Lambda 로 함께 배포.
