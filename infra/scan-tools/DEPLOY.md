# scan-tools 배포 런북 (SP-3)

## 사람 게이트
이미지 빌드는 Docker Desktop **amazonians 조직 로그인**(GUI)이 필요해요. 무인 실행 불가.

## 순서
1. Docker Desktop 로그인 확인.
2. CDK 배포(Lambda·Fargate·ECR repo·VPC 생성):
   ```
   cd infra && npm run check:cognito -- --stage dev && \
     npx cdk deploy AgoraGovernanceScanTools-dev -c stage=dev
   ```
   - **lambda 도구(gitleaks)**: 이미지 에셋이라 `cdk deploy`가 CDK 에셋 repo(`cdk-hnb659fds-container-assets-*`)로 빌드·push 를 **자동** 처리해요. per-tool ECR repo는 만들지 않아요.
   - **fargate 도구(semgrep)**: per-tool ECR repo(`agora-tool-semgrep-dev`)만 생성돼요. 비어 있으니 3번으로 직접 push 해야 해요.
3. Fargate 도구(semgrep)만 이미지 push (`build-and-push.sh`는 fargate 전용):
   ```
   ./scan-tools/build-and-push.sh semgrep dev <ACCOUNT>
   ```
4. 스택 Outputs 확인: `Tool*FunctionArn`, `Tool*TaskDefArn`, `ScanBucketName`, `IsolatedSubnetIds`, `ScanSecurityGroupId`, `ClusterName`.
5. (SP-4) 이 Outputs를 Step Functions·앱 env로 배선.

## teardown (상시비 절감)
```
cd infra && npx cdk destroy AgoraGovernanceScanTools-dev -c stage=dev
```
Interface EP(ECR/Logs)가 상시비의 대부분 — 검증 후 반드시 destroy.

## air-gap 검증
`npm test -- scan-tools-stack` — IGW:0·NAT:0·IAM Action:* 없음·Lambda VPC 배치.

## SP-4 오케스트레이션 (Step Functions + EventBridge)

배포 후 오케스트레이션 흐름:
1. `cdk deploy AgoraGovernanceScanTools-dev` — SF 상태머신(`agora-scan-orchestration-dev`)·aggregate Lambda(`agora-scan-aggregate-dev`)·EventBridge 규칙(`agora-scan-requested-dev`) 포함.
2. 앱 env 배선: `AGORA_SCANNER=stepfn`, `AGORA_SFN_ARN=<StateMachineArn output>`, `AGORA_SCAN_BUCKET=<ScanBucketName output>`, `AGORA_SCAN_REGION=ap-northeast-2`.
3. 스캔 트리거: 앱이 `StepFunctionScanRunner`로 `start_execution`(멱등 name=scan_id) 또는 EventBridge에 `{source:"agora.governance", detail-type:"ScanRequested"}` 이벤트 발행.

### P1 골격의 한계 (배포 e2e에서 정밀화 — 사람 게이트)
- SF Map 브랜치는 현재 **pass-through**(실제 도구 lambda:invoke/ecs:runTask.sync 미통합). 실 도구 실행은 도구 ARN 동적 라우팅을 배포 후 정밀화. fargate 도구 호출 시 컨테이너명 **`scanner`** + env `INPUT_URI/OUTPUT_URI/SCAN_ID/SCAN_TIMEOUT`, lambda 도구는 event `{input_uri,output_uri,scan_id}`.
- `StepFunctionScanRunner`는 P1에서 "표준" 매트릭스로 도구목록 구성(자산 타입별 등급 반영은 scan_service가 tier 전달하도록 후속 P2).
- SF 완료→앱 done 전이(콜백/폴링)는 후속. P1은 start_execution + running까지.

### teardown
```
cd infra && npx cdk destroy AgoraGovernanceScanTools-dev -c stage=dev
```
SF·EventBridge는 종량(상시비 미미), Interface EP(ECR/Logs)가 상시비 대부분 — 검증 후 destroy.

## SP-4 P2 — 실 도구 실행 e2e (사람 게이트)

P2로 SF Map이 dispatcher Lambda를 통해 실제 도구를 실행해요. e2e 순서:
1. `cd infra && npm run check:cognito -- --stage dev && npx cdk deploy AgoraGovernanceScanTools-dev -c stage=dev` — dispatcher Lambda(`agora-scan-dispatch-dev`) 포함 배포. gitleaks Lambda 이미지는 cdk가 자동 빌드·push.
2. semgrep Fargate 이미지 push: `./scan-tools/build-and-push.sh semgrep dev <ACCOUNT>`.
3. 스택 Outputs 확인: `StateMachineArn`, `ScanBucketName`. (dispatcher가 쓰는 cluster/subnets/sg는 dispatcher env로 이미 배선됨.)
4. 앱 env 전환 후 API 재시작:
   ```
   AGORA_SCANNER=stepfn
   AGORA_SFN_ARN=<StateMachineArn output>
   AGORA_SCAN_BUCKET=<ScanBucketName output>
   AGORA_SCAN_REGION=ap-northeast-2
   ```
5. 브라우저 e2e: 위키 시크릿 SKILL.md(`02-Internal/Projects/Agora/test/data/deploy-notifier-skill/SKILL.md`)를 skill로 카탈로그 등록 → 승인 큐에서 스캔 실행 →
   - skill=최소 등급 → gitleaks(Lambda, 시크릿 검출)·semgrep(Fargate) 진짜 실행, snyk(이미지 없음)→미실행(not_run)
   - scan_status 폴링(3초)이 describe_execution으로 SF 완료를 done으로 전이 → 게이트에 실검출 반영.
6. teardown: `npx cdk destroy AgoraGovernanceScanTools-dev -c stage=dev`.

### e2e 검증 포인트
- Step Functions 콘솔: `agora-scan-orchestration-dev` 실행 그래프(Map 브랜치별 dispatcher → aggregate).
- CloudWatch: `agora-scan-dispatch-dev`(dispatcher) 로그, `agora-tool-gitleaks-dev`(Lambda) 호출, `agora-tool-semgrep-dev`(Fargate) 태스크.
- 앱 상세화면: 시크릿 SKILL.md → gitleaks FAIL(검출), semgrep 판정, snyk 미실행(not_run) → 게이트 판정.
