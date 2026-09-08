# QUICKSTART

코드를 읽고 검증하고 합성(`cdk synth`)한 뒤 계정에 배포하는 절차. 어느 계정에 무엇을 만들지는
읽는 쪽의 판단 영역이고, 배포 전 [계정 선행조건](#계정-선행조건) 네 가지를 먼저 확인.

| 목적 | 절 |
| --- | --- |
| 읽고 검증만 | [1. 의존성 설치](#1-의존성-설치) → [2. 검증](#2-검증) → [3. CDK 합성](#3-cdk-합성) |
| 로컬 기동 | [4. 로컬 실행](#4-로컬-실행) |
| 계정에 배포 | [계정 선행조건](#계정-선행조건) → [5. 배포](#5-배포) |

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

## 계정 선행조건

합성이 아니라 **배포**에만 해당. 넷 다 계정·리전 수준 설정이라 CDK 가 만들어 주지 않고, 빠지면
스택이 CREATE_FAILED 로 죽거나 포털 기능이 조용히 막힘.

### 1) X-Ray trace segment destination = CloudWatch Logs

`AgoraM2OAuthGateway` 의 `AWS::Logs::Delivery` 와 `AgoraMonitoringAggregate` 의 `aws/spans`
subscription filter 가 이 설정에 의존. 기본값(`XRay`)이면 두 스택이 CREATE_FAILED.

```bash
ACCOUNT=<12자리>
REGION=ap-northeast-2

# (1) 리소스 정책 먼저. 이게 없으면 (2)가 AccessDenied.
cat > /tmp/xray-spans-policy.json <<EOF
{"Version":"2012-10-17","Statement":[{
  "Sid":"TransactionSearchXRayAccess","Effect":"Allow",
  "Principal":{"Service":"xray.amazonaws.com"},
  "Action":["logs:PutLogEvents","logs:CreateLogStream"],
  "Resource":[
    "arn:aws:logs:${REGION}:${ACCOUNT}:log-group:aws/spans:*",
    "arn:aws:logs:${REGION}:${ACCOUNT}:log-group:/aws/application-signals/data:*"],
  "Condition":{
    "ArnLike":{"aws:SourceArn":"arn:aws:xray:${REGION}:${ACCOUNT}:*"},
    "StringEquals":{"aws:SourceAccount":"${ACCOUNT}"}}}]}
EOF
aws logs put-resource-policy --region $REGION \
  --policy-name TransactionSearchXRayAccess \
  --policy-document file:///tmp/xray-spans-policy.json

# (2) 전환. Status 가 PENDING → ACTIVE 될 때까지 2~3분 대기.
aws xray update-trace-segment-destination --region $REGION --destination CloudWatchLogs
aws xray get-trace-segment-destination --region $REGION
```

### 2) VPCs per Region 쿼터

앱이 VPC 를 3개 생성(`AgoraGovernanceScan`, `AgoraGovernanceScanTools`, `AgoraPortal`).
계정 default VPC 와 기존 워크로드를 합치면 기본 한도 5 를 넘김. 8 이상 확보 권장.

```bash
aws service-quotas get-service-quota --service-code vpc --quota-code L-F678F1CE
aws service-quotas request-service-quota-increase \
  --service-code vpc --quota-code L-F678F1CE --desired-value 10
```

한도 초과 시 증상은 `AgoraPortal-<stage>` 의
`PortalVpc … The maximum number of VPCs has been reached.`

### 3) 사람 사용자 최소 2명

퍼블리시 폼의 "2차 담당자(에스컬레이션)" 가 필수이고 본인 선택 불가. Cognito 사용자가 1명이면
어떤 자산도 등록 불가. `AgoraIdentity` 배포 후 관리자 1명 + 다른 구성원 1명 이상 생성.

### 4) semgrep 스캐너 이미지 push

semgrep 은 Fargate 도구라 CDK 가 이미지를 만들지 않음. 비어 있으면 태스크가
`CannotPullContainerError` 로 죽고 스캔이 SAST 단계에서 실패.

```bash
cd infra/scan-tools
./build-and-push.sh semgrep dev <account-id> ap-northeast-2
```

`AgoraGovernanceScanTools` 배포 뒤(ECR 리포지토리 생성 후) 실행. 이미지는 `linux/amd64`.

## 1. 의존성 설치

```bash
cd api  && uv sync --extra dev --extra aws
cd ../web   && npm ci
cd ../infra && npm ci
```

## 2. 검증

```bash
cd api   && uv run ruff check . && uv run pytest -q tests/
cd ../web   && npm run lint && npm run build
cd ../infra && npm run build
```

`infra/` 의 스캔 도구·오케스트레이터 테스트는 모듈 옆에 붙어 있고 외부 pytest 로 실행
(`pytest`, `moto` 필요). **디렉터리별로** 실행해야 함. `scan-runner` 와 `scan-tools/shared` 에
`normalize.py`·`test_normalize.py` 가 같은 이름으로 있어서, 한 번에 수집하면 모듈이 서로
가려져 통과할 테스트가 실패로 보임.

```bash
cd infra
for d in scan-runner scan-tools/shared scan-tools/gitleaks \
         scan-tools/llm-judge scan-tools/orchestrator scripts; do
  (cd "$d" && pytest -q .)
done
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

# workload M2M 좌표. AgoraRuntimeDeploy 의 Cognito* output 세트에서 가져옴.
# 미설정이면 그 스택 출력을 cross-stack ref 로 가져오므로 합성만 볼 때는 생략 가능.
export AGORA_DEPLOY_COGNITO_DISCOVERY_URL=<AgoraRuntimeDeploy CognitoDiscoveryUrl>
export AGORA_DEPLOY_COGNITO_CLIENT_ID=<AgoraRuntimeDeploy CognitoClientId>
export AGORA_DEPLOY_COGNITO_SCOPE=https://agora-mcp-<stage>/invoke

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

## 5. 배포

[계정 선행조건](#계정-선행조건) 네 가지를 먼저 확인. 스택 의존 순서는 `cdk deploy --all` 이
해석하므로 손으로 순서를 지정할 필요 없음. `cdk.out/manifest.json` 기준 그래프.

```
AgoraRuntimeDeploy ─┐
AgoraM2OAuthGateway ┴→ AgoraCatalogStorage → AgoraIdentity → AgoraRuntimeAuthorization
AgoraGovernanceScan       (독립)
AgoraGovernanceScanTools  (독립)
AgoraPortal (-c portal=true) → Identity 의 Cognito callback 이 이 스택의 CloudFront 를 참조
```

### 신규 계정 첫 배포 — 2-pass 부트스트랩

합성이 요구하는 값 일부가 **배포 후에만** 생기는 순환이 있음. `--exclusively` 를 붙여도 앱 전체를
합성하므로 무관한 스택 하나만 배포할 때도 아래 값이 전부 필요.

| 값 | 출처 | 순환 |
| --- | --- | --- |
| `AGORA_GATEWAY_HUMAN_CLIENT_IDS` | `AgoraIdentity` 의 `HumanCognitoClientId` | Identity 는 CatalogStorage → M2OAuthGateway 뒤 |
| `AGORA_M2_OAUTH_COGNITO_USER_POOL_ID`·`AGORA_M2_OAUTH_DISCOVERY_URL` | Gateway 인바운드 발급자 pool | 같음 |
| `AGORA_REGISTRY_ID` | AWS Agent Registry | 백엔드가 만들지만 그 백엔드를 띄우려면 값이 필요 |

**pass 1** — 형식만 맞는 임시값으로 전체 배포.

```bash
# 임시 Cognito pool 하나. Gateway authorizer 의 discoveryUrl 이 실재해야 생성이 통과.
POOL=$(aws cognito-idp create-user-pool --region ap-northeast-2 \
  --pool-name agora-bootstrap-placeholder --query UserPool.Id --output text)

# Registry 는 SDK 로 미리 생성. 이름은 백엔드가 찾는 `agora-registry` 로 고정.
python - <<'PY'
import boto3
r="us-east-1"; s="agent-registry-control"
c=boto3.client(s, region_name=r, endpoint_url=f"https://{s}.{r}.api.aws")
name="agora-registry"
hit=[x for x in c.list_registries().get("registries",[]) if x["name"]==name]
arn=hit[0]["registryArn"] if hit else c.create_registry(
    name=name, description=name, approvalConfiguration={"autoApprovalRules": []})["registryArn"]
print(arn.rsplit("/",1)[-1])
PY

export AGORA_GATEWAY_HUMAN_CLIENT_IDS=bootstrapplaceholderclient
export AGORA_M2_OAUTH_COGNITO_USER_POOL_ID=$POOL
export AGORA_M2_OAUTH_DISCOVERY_URL=https://cognito-idp.ap-northeast-2.amazonaws.com/$POOL/.well-known/openid-configuration
export AGORA_REGISTRY_ID=<위 스크립트가 출력한 registryId>

npx cdk deploy --all -c stage=dev
```

**pass 2** — 실제 output 으로 값을 교체하고 포털까지 배포. 그 뒤 임시 pool 삭제.

```bash
npx cdk deploy --all -c stage=dev -c portal=true
aws cognito-idp delete-user-pool --region ap-northeast-2 --user-pool-id $POOL
```

포털 CloudFront 도메인이 나오면 `AGORA_PORTAL_ORIGIN` 에 넣고 `AgoraCatalogStorage` 만 한 번 더
배포. 소스 S3 버킷 CORS 에 포털 origin 이 들어가야 브라우저 업로드가 통과.

### CDK output → 환경변수 매핑

이름이 비슷해서 자리를 바꿔 넣기 쉬운 것들. 실행롤 셋은 PassRole grant 가 `iam:PassedToService`
로 갈라져 있어서 잘못 넣으면 조용히 넘어가지 않고 배포가 AccessDenied 로 실패.

| 스택 output | 환경변수 | 실제 대상 |
| --- | --- | --- |
| `LambdaExecRoleArn` | `AGORA_DEPLOY_EXEC_ROLE_ARN` | MCP tool-provider **Lambda** |
| `ExecRoleArn` = `McpRuntimeExecRoleArn` | (전용 env 없음) | AgentCore Runtime (MCP) |
| `AgentRuntimeExecRoleArn` | `AGORA_DEPLOY_AGENT_EXEC_ROLE_ARN` | AgentCore Runtime (agent) |
| `HumanCognitoClientId` | `AGORA_AUTH_COGNITO_CLIENT_ID`·`AGORA_GATEWAY_HUMAN_CLIENT_IDS` | 사람 web client |
| `M2OAuthCognitoUserPoolId` | `AGORA_M2_OAUTH_COGNITO_USER_POOL_ID` | agent 별 M2M client 발급 pool |
| `StateMachineArn` (ScanTools) | `AGORA_SFN_ARN` | 스캔 오케스트레이션 |

`AGORA_DEPLOY_EXEC_ROLE_ARN` 에 `ExecRoleArn` 을 넣으면 MCP(배포형) 등록이
`iam:PassRole … McpRuntimeExecRole … no identity-based policy allows` 로 실패.

### `AGORA_DEPLOY_COGNITO_*` 를 처음 넣는 배포

`AgoraRuntimeAuthorization` 은 이 네 값이 없으면 `AgoraRuntimeDeploy` 출력을 cross-stack ref 로
가져옴. 나중에 env 로 채우면 import 가 사라지는데 `cdk deploy --all` 은 producer 를 먼저 배포해서
아직 쓰이는 export 를 지우려 하고, `Cannot delete export … as it is in use by` 로 롤백.

consumer 를 먼저 배포하면 해결.

```bash
npx cdk deploy AgoraRuntimeAuthorization-dev --exclusively -c stage=dev
npx cdk deploy --all -c stage=dev -c portal=true
```

### 배포 전 확인 항목

- 합성된 `AWS::Lambda::Permission` 에 `Principal: "*"` 부재. Runtime 인가 Function URL 은
  `AuthType=AWS_IAM` 유지.
- Gateway policy engine 모드가 `ENFORCE` 인지 확인. 플래그 미전달 시 `ENFORCE` 가 기본이지만,
  `cdk diff` 에 `ENFORCE → LOG_ONLY` 혼입 여부는 육안 확인.
- `AgoraGovernanceScanTools` 는 VPC 조회를 수행하므로 합성에 크리덴셜 필요.
- 배포 후 `cdk deploy` 로 올린 task definition 리비전이 올라갔는지 확인. 안 올랐으면 라이브는
  아직 이전 이미지.

### 배포 후 첫 사용

1. `AgoraIdentity` 의 Cognito 풀에 사용자 2명 이상 생성하고 `admin`·`user` 그룹 배정.
2. 포털 CloudFront URL 로 로그인 → 카탈로그가 뜨는지 확인.
3. MCP·Agent 를 등록하면 배포가 끝난 뒤 승인 큐에 진입. 스캔 4단계(gitleaks·semgrep·trivy·
   LLM 심사)를 통과하면 카탈로그에 노출.
4. 배포된 도구의 호출 권한은 관리자 콘솔 › 도구 인가 승인에서 별도 승인. 승인 전 호출은 거부.

## 환경변수 계약

두 부류.

`[S]` 는 공유 데이터·인프라 좌표. 로컬과 호스팅 포털이 같은 리소스를 가리키게 하는 값이고 CDK
output 에서 전달.

`[E]` 는 환경별 운영 정책. 프로세스별 소유 값이라 포털 배포로 복사되지 않고 `AgoraPortal`
스택이 명시적으로 주입.

`api/.env.example` 이 부류 표시와 함께 전체 목록 보유. 백엔드 변수는 `AGORA_*`, 브라우저 노출
변수는 `NEXT_PUBLIC_*` 접두어. 크리덴셜 원문은 Secrets Manager 또는 SSM 에 두고 소스·예시
파일에 미포함.

Registry 네임스페이스(`AGORA_REGISTRY_NAMESPACE`)는 `agent-registry` 가 기본. CDK 가 부여하는
registry IAM grant 도 이 네임스페이스 기준이고, endpoint 는 `.api.aws` 도메인. 구
`bedrock-agentcore` 네임스페이스는 되돌리기 좌표로만 남겨 둔 값이라 새 계정에서 쓰지 않음.
