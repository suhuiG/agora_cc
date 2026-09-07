import * as cdk from "aws-cdk-lib";

export type CognitoDomainPurpose = "human" | "m2-oauth" | "mcp-gateway";

const DOMAIN_BASE: Record<CognitoDomainPurpose, string> = {
  human: "agora-human",
  "m2-oauth": "agora-m2-oauth",
  "mcp-gateway": "agora-mcp",
};

// Cognito 도메인 접두어는 **전역 유일**이에요. 이미 배포된 환경의 접두어를 보존해야 하면
// `-c cognitoDomainPrefix:<purpose>:<stage>=<prefix>` 로 주입해요. 아무것도 핀하지 않으면
// 아래 `cognitoDomainPrefix` 가 계정 ID 를 붙여 충돌하지 않는 접두어를 만들어요.
const DEPLOYED_PREFIXES: Readonly<Record<string, string>> = {};

const USER_POOL_BASE: Record<CognitoDomainPurpose, string> = {
  human: "agora-human",
  "m2-oauth": "agora-m2-oauth",
  "mcp-gateway": "agora-mcp-gateway",
};

// CreateUserPoolDomain API contract:
// https://docs.aws.amazon.com/cognito-user-identity-pools/latest/APIReference/API_CreateUserPoolDomain.html
const PREFIX_PATTERN = /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/;
const RESERVED_TERMS = ["aws", "amazon", "cognito"];

function validateDomainPrefix(prefix: unknown, contextKey: string): string {
  if (
    typeof prefix !== "string" ||
    !PREFIX_PATTERN.test(prefix) ||
    RESERVED_TERMS.some((term) => prefix.includes(term))
  ) {
    throw new Error(
      `invalid Cognito domain prefix in context '${contextKey}': ` +
      "expected 1-63 lowercase letters, numbers, or hyphens; " +
      "no leading/trailing hyphen or reserved term aws, amazon, cognito",
    );
  }
  return prefix;
}

for (const [key, prefix] of Object.entries(DEPLOYED_PREFIXES)) {
  validateDomainPrefix(prefix, `DEPLOYED_PREFIXES['${key}']`);
}

export function cognitoDomainPrefix(
  stack: cdk.Stack,
  stage: string,
  purpose: CognitoDomainPurpose,
): string {
  const contextKey = `cognitoDomainPrefix:${purpose}:${stage}`;
  const injectedPrefix = stack.node.tryGetContext(contextKey);
  if (injectedPrefix !== undefined) {
    return validateDomainPrefix(injectedPrefix, contextKey);
  }

  const deployedPrefix = DEPLOYED_PREFIXES[`${purpose}:${stage}`];
  if (deployedPrefix !== undefined) {
    return validateDomainPrefix(
      deployedPrefix,
      `DEPLOYED_PREFIXES['${purpose}:${stage}']`,
    );
  }

  return `${DOMAIN_BASE[purpose]}-${stage}-${cdk.Aws.ACCOUNT_ID}`;
}

export function cognitoUserPoolName(
  stage: string,
  purpose: CognitoDomainPurpose,
): string {
  return `${USER_POOL_BASE[purpose]}-${stage}`;
}

/**
 * M2 OAuth Gateway resource server identifier — the `https://agora-m2-oauth-<stage>`
 * string. Both the M2 OAuth Gateway stack (which registers the resource server and
 * feeds the Gateway `allowedScopes`) and the Identity stack (which registers the same
 * resource server on the human pool, IA-75 ①) derive it from here so the two never drift.
 *
 * A pure stage→coordinate helper on the same pattern as `identityDataTableName`. It is a
 * helper rather than a cross-stack prop because IdentityStack is created before
 * M2OAuthGatewayStack, and an M2→Identity Ref would close the
 * Catalog→M2→Identity→Portal→Catalog CloudFormation cycle (see `bin/agora.ts` comments).
 */
export function m2OAuthResourceServerIdentifier(stage: string): string {
  return `https://agora-m2-oauth-${stage}`;
}

/** The `.../invoke` scope built from {@link m2OAuthResourceServerIdentifier}. */
export function m2OAuthInvokeScope(stage: string): string {
  return `${m2OAuthResourceServerIdentifier(stage)}/invoke`;
}
