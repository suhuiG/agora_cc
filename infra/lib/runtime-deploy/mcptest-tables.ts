import { Stack } from "aws-cdk-lib";

/**
 * CS 핸즈온 데모 MCP 세 개가 읽고 쓰는 DynamoDB 테이블.
 *
 * `sample/user-table-mcp` · `item-table-mcp` · `order-table-mcp` 의 실체예요.
 * 테이블은 `sample/mcptest-tables/provision_mcptest_tables.py` 가 만들고, 여기서는
 * **권한 대상으로만** 참조해요.
 *
 * ## 왜 별도 모듈인가
 *
 * 이 목록은 두 곳에서 같아야 해요:
 *   1. `iam-roles.ts` — McpLambdaExecRole 의 인라인 CRUD 정책(실제 부여)
 *   2. `permission-boundary.ts` — boundary 의 허용 상한과 삭제 예외(천장)
 *
 * 유효 권한은 **정책 ∩ boundary** 라, 한쪽만 고치면 조용히 아무 권한도 안 생겨요
 * (2026-08-30 실측: 인라인만 추가했더니 `AccessDeniedException` 이 그대로 났어요).
 * 그래서 목록을 여기 한 번만 적고 양쪽이 가져다 써요.
 */
export const MCPTEST_TABLE_KINDS = ["user", "item", "order"] as const;

/**
 * 테이블 본체 + 인덱스(`email-index` 등) ARN 6개.
 *
 * **부여(role 인라인 정책)** 쪽에서 써요 — 정확히 열거해서, 나중에 다른 테이블이 슬쩍
 * 끼어드는 걸 회귀 테스트로 잡을 수 있게요.
 */
export function mcptestTableArns(stack: Stack, stage: string): string[] {
  return MCPTEST_TABLE_KINDS.flatMap((kind) => {
    const table =
      `arn:${stack.partition}:dynamodb:${stack.region}:${stack.account}` +
      `:table/agora-mcptest-${kind}-${stage}`;
    return [table, `${table}/index/*`];
  });
}

/**
 * 같은 테이블 집합을 ARN **하나**로 표현한 형태.
 *
 * `permission-boundary.ts` 전용이에요. managed policy 는 6,144자 상한이 있고 boundary 는
 * 이미 그 근처라, 위 6개를 두 문장에 넣으면 `ServiceLimitExceeded` 로 배포가 깨져요
 * (2026-08-30 실측). boundary 는 **천장**이라 부여보다 거칠어도 안전해요 — 실제 권한은
 * 열거된 role 인라인 정책과의 교집합이니까요.
 *
 * 뒤쪽 `*` 하나가 인덱스 ARN(`.../index/email-index`)까지 덮어요. `-${stage}` 를 패턴에
 * 남겨서 stage 경계는 지켜요 — `agora-mcptest-*` 로 줄이면 dev boundary 가 prod 테이블까지
 * 천장에 올려요.
 */
export function mcptestTableArnPattern(stack: Stack, stage: string): string {
  return (
    `arn:${stack.partition}:dynamodb:${stack.region}:${stack.account}` +
    `:table/agora-mcptest-*-${stage}*`
  );
}
