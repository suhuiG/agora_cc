# Agora: AgentCore 거버넌스 포털 (reference code)

Amazon Bedrock AgentCore 위에서 Skill·MCP·Agent 자산을 등록하고, 심사·승인하고, 누가 어떤
도구를 호출할 수 있는지 판정하는 포털의 참조 구현이에요.

고객 공유용으로 정리한 사본이라 원본 개발 저장소의 내부 문서, 운영 기록, 라이브 리소스
좌표는 들어 있지 않아요. 빠진 범위는 아래 [무엇이 빠져 있나](#무엇이-빠져-있나)에 적어 뒀어요.

## 무엇을 하는 코드인가

한 바퀴는 등록에서 시작해 스캔, 승인, 배포를 지나 호출로 끝나요.

1. 등록자가 자산을 올리면 AWS Agent Registry 에 레코드가 생기고 승인 큐로 들어가요.
2. 거버넌스 파이프라인이 보안 스캔을 돌려요. 시크릿(gitleaks), SAST(semgrep), 취약점(trivy),
   LLM 심사를 Step Functions 가 오케스트레이션하고 Lambda 와 Fargate 가 실행해요.
3. 관리자가 게이트 결과를 보고 승인하면 자산이 카탈로그에 노출돼요.
4. MCP 는 Lambda + AgentCore Gateway Target 으로, Agent 는 AgentCore Runtime 으로 배포돼요.
5. 배포된 agent 가 도구를 부르면 Gateway 의 REQUEST interceptor 가 판정해요.

## 도구 인가는 두 층이에요

이 저장소를 읽을 때 먼저 볼 곳이에요.

| 층 | 질문 | 저장 위치 |
| --- | --- | --- |
| 에이전트 도구 승인 | 이 agent 가 이 도구를 부를 수 있나 | Identity 원장 (DynamoDB) |
| 사용자 권한 부여 | 이 사람·그룹이 이 자산의 이 작업을 부를 수 있나 | Identity 원장, `(subject, asset_id, operation_id)` 키 |

두 층 다 fail-closed 예요. 승인 레코드가 없으면 거부하고, 거부 이유를 호출자에게 그대로
돌려줘요 (`tool_not_approved`, `tool_pending_approval`, `human_grant_missing`).

Cedar 정책은 Gateway 인프라의 일부예요. agent 를 하나 더 배포해도 정책은 늘지 않아요. Gateway
당 정책 한 장이 선언된 도구 이름(`${target}___${tool}`)을 열거하고, agent 별 차이는 원장이
결정해요. agent 신원(`client_id`)은 정책 문장에 절대 들어가지 않아요.

판정 지점은 interceptor 하나예요. Cedar 는 그 뒤에서 interceptor 가 다시 쓴 본문을 평가하니까,
독립적인 2차 방어선으로 취급하면 안 돼요.

## 저장소 구조

```
api/      FastAPI 백엔드. 도메인별 패키지(catalog, governance, identity, runtime,
          playground, monitoring, bundle, evaluation)와 공용 DI(shared/deps.py)
web/      Next.js 16 포털. 사용자 화면((portal))과 관리자 콘솔((console)/admin)
infra/    AWS CDK. 스택 8종, 스캔 도구 컨테이너, Gateway 타깃 Lambda
```

도메인 경계가 오너십 경계예요. 한 도메인이 다른 도메인을 직접 import 하지 않고, 공유가
필요하면 `api/src/agora/shared/` 로 올려요. 카탈로그 상태는 `RegistryPort`,
`SourceStorePort`, `shared.deps` 접근자로만 만져요.

### CDK 스택

| 스택 | 담당 |
| --- | --- |
| `AgoraCatalogStorage` | 카탈로그 DynamoDB·S3, API 실행 롤 |
| `AgoraGovernanceScan` | 스캔 Step Functions, 스캔 버킷 |
| `AgoraGovernanceScanTools` | gitleaks, semgrep, trivy, LLM 심사 실행체 |
| `AgoraRuntimeDeploy` | MCP·Agent 배포 파이프라인, CodeBuild, 실행 롤, permission boundary |
| `AgoraIdentity` | 사람 Cognito 풀, 세션 테이블, Identity 원장 |
| `AgoraM2OAuthGateway` | AgentCore Gateway, Cedar policy engine, REQUEST interceptor |
| `AgoraRuntimeAuthorization` | Runtime 인가 Lambda (Function URL, `AuthType=AWS_IAM`) |
| `AgoraPortal` | ECS Fargate + ALB + CloudFront 호스팅 (`-c portal=true` 일 때만) |

`AgoraMonitoringAggregate` 와 `AgoraTelemetryArchive` 는 컨텍스트 플래그로 켜는 선택 스택이에요.

## 안전 기본값

배포하기 전에 이 넷을 확인해요. 참조 코드에서 이미 안전한 쪽으로 맞춰 뒀고, 되돌리려면
명시적인 플래그가 필요해요.

Gateway policy engine 은 `ENFORCE` 가 기본이에요. `LOG_ONLY` 는 `-c
m2OAuthGatewayMode=LOG_ONLY` 를 명시할 때만 돼요. `LOG_ONLY` 에서 관측한 "거부 0건" 은 강제가
켜져 있다는 증거가 아니에요.

REQUEST interceptor 도 기본으로 붙어요. `-c m2OAuthInterceptor=off` 로만 뗄 수 있어요.
interceptor 의 가용성이 곧 Gateway 의 가용성이라, 지우거나 IAM 권한을 떼거나 reserved
concurrency 를 내리면 도구 인가가 통째로 멈춰요.

Runtime 인가 Function URL 은 `AuthType=AWS_IAM` 을 유지해야 하고, Lambda 리소스 정책의
principal 이 `"*"` 이면 안 돼요. 합성된 `AWS::Lambda::Permission` 과 실제 리소스 정책을 배포
전에 둘 다 보세요.

Cognito 도메인 접두어는 전역 유일해요. 그래서 이 코드는 접두어에 계정 ID 를 붙여 파생해요
(`<base>-<stage>-<accountId>`). 이미 배포된 환경의 접두어를 보존해야 하면
`-c cognitoDomainPrefix:<purpose>:<stage>=<prefix>` 로 주입해요.

AWS 좌표(계정, 풀 ID, Gateway ID, 테이블명)는 소스에 하나도 박혀 있지 않아요. 전부 환경변수
또는 CDK 컨텍스트로 들어가고, 없으면 부팅과 합성이 실패해요. `api/.env.example` 과
`web/.env.example` 이 그 계약의 템플릿이에요.

## 시작하기

[QUICKSTART.md](QUICKSTART.md) 에 로컬 실행, 검증 명령, `cdk synth` 까지의 절차가 있어요.

## 무엇이 빠져 있나

고객 공유본이라 아래를 뺐어요. 코드를 읽다가 참조가 끊긴 곳이 보이면 이 목록 때문이에요.

설계 문서는 전부 뺐어요. ADR, 설계 스펙, 연구 노트, 운영 런북, 백로그가 여기 해당해요. 코드
주석에 남은 `ADR-00NN`, `IH-NNN`, `docs/...` 표기는 그 문서를 가리키던 흔적이에요. 결정의
이유는 대부분 주석 본문에 그대로 적혀 있어요.

폐기된 실행 경로도 뺐어요. AgentCore Harness 실행 모드(도메인, 라우터, 스택, UI 전부), agent
별 Cedar 정책 배포, Cedar/IAM principal 검증용 스파이크 스택 3종이에요.

라이브 운영 산출물은 일회성 마이그레이션·정리 스크립트, 배포 런북, 검증 체크리스트, 워크샵
콘텐츠, 샘플 MCP·agent 예요.

테스트 스위트(API 300여 개, 인프라 233개, 웹 계약 테스트)도 뺐어요. 이 사본을 만들 때 전부
통과한 상태로 잘라냈어요.

예외가 하나 있어요. `api/src/agora/domains/identity/agent_policy_cutover.py` 는 폐기 표시가
붙어 있는데도 남겨 뒀어요. 살아 있는 정책 provisioning 코드가 이 모듈의 타입을 쓰고 있어서,
떼어내려면 인가 경로를 다시 설계해야 하거든요. 모듈 docstring 이 폐기 이유와 되살릴 조건을
설명해요.

## 사용 범위

AWS 고객 참조용으로 공유한 코드예요. 그대로 프로덕션에 올리지 말고, 위 안전 기본값을 확인하고
자기 계정의 보안 요구사항에 맞춰 검토한 뒤 쓰세요.
