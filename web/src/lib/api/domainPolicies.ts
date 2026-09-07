// 도메인 규칙 Cedar 정책 admin API 클라이언트 (IH-132).
//
// Cedar 문장은 **서버가 조립해요.** 이 모듈은 구조화된 값(자산·도구·인자·연산자·임계값)만
// 보내고, 미리보기로 「서버가 만들 문장」을 받아 화면에 그대로 보여줘요. 클라이언트가 Cedar
// 원문을 보내는 경로는 없어요 — 있으면 인가 문장을 클라이언트가 쓰는 셈이에요.
//
// 화면 문구를 만드는 순수 함수를 여기 함께 둬요. 컴포넌트에 두면 테스트할 수 없거든요
// (web/ 에는 렌더 테스트 러너가 없어요).

import { JSON_HEADERS, request } from "./client.ts";

const BASE = "/api/admin/identity/domain-policies";

export type EnforcementMode = "LOG_ONLY" | "ACTIVE" | "";

export interface ToolArgument {
  name: string;
  json_type: string;
  required: boolean;
  /** Cedar 식별자로 못 쓰는 이름이면 false. 고를 수 없어요. */
  usable: boolean;
  reason: string;
}

export interface ToolOption {
  tool_name: string;
  target_name: string;
  sensitivity: string;
  description: string;
  arguments: ToolArgument[];
  /** false 는 스키마를 읽지 못했다는 뜻이에요. 인자 0개와 다른 상태예요. */
  schema_observed: boolean;
  schema_reason: string;
}

export interface AssetOption {
  asset_id: string;
  asset_version: string;
  name: string;
  tools: ToolOption[];
  reason: string;
}

export interface DomainPolicyOptions {
  gateway_arn: string;
  assets: AssetOption[];
  warnings: string[];
  numeric_operators: string[];
  string_operators: string[];
}

export interface CoarseConflict {
  policy_id: string;
  policy_name: string;
  kind: string;
  label: string;
  detail: string;
}

export interface ConflictObservation {
  /** false 는 관측 실패예요. 「충돌 없음」과 구분해야 해요. */
  observed: boolean;
  reason: string;
  conflicts: CoarseConflict[];
}

export interface DomainPolicyPreview {
  action: string;
  cedar: string;
  policy_hash: string;
  size_bytes: number;
  gateway_arn: string;
  conflict: ConflictObservation;
}

export interface EnforcementChange {
  requested_mode: string;
  observed_mode: string;
  observed_status: string;
  changed_by: string;
  changed_at: string;
  reason: string;
}

export interface DomainPolicyRule {
  rule_id: string;
  gateway_arn: string;
  engine_id: string;
  asset_id: string;
  asset_version: string;
  target_name: string;
  tool_name: string;
  gateway_action: string;
  argument: string;
  operator: string;
  value_kind: string;
  threshold: string;
  cedar_policy: string;
  policy_hash: string;
  remote_policy_id: string;
  remote_policy_name: string;
  observed_status: string;
  created_by: string;
  created_at: string;
  description: string;
  status_reasons: string[];
  coarse_conflicts: string[];
  coarse_conflict_policies: string[];
  conflict_acknowledged: boolean;
  enforcement_mode: EnforcementMode;
  requested_enforcement_mode: string;
  enforcement_changes: EnforcementChange[];
  version: number;
}

export interface DomainPolicyList {
  gateway_arn: string;
  rules: DomainPolicyRule[];
}

export interface DomainRuleDraft {
  asset_id: string;
  asset_version: string;
  target_name: string;
  tool_name: string;
  argument: string;
  operator: string;
  value_kind: "number" | "string";
  value: string;
  description: string;
}

export function getDomainPolicies(): Promise<DomainPolicyList> {
  return request<DomainPolicyList>(BASE);
}

export function getDomainPolicyOptions(): Promise<DomainPolicyOptions> {
  return request<DomainPolicyOptions>(`${BASE}/options`);
}

export function previewDomainPolicy(
  draft: DomainRuleDraft,
): Promise<DomainPolicyPreview> {
  return request<DomainPolicyPreview>(`${BASE}/preview`, {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify(draft),
  });
}

export function createDomainPolicy(
  draft: DomainRuleDraft,
  acknowledgeIneffective: boolean,
): Promise<DomainPolicyRule> {
  return request<DomainPolicyRule>(BASE, {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify({
      ...draft,
      acknowledge_ineffective: acknowledgeIneffective,
    }),
  });
}

/** LOG_ONLY → ACTIVE. 모드를 인자로 받지 않아요 — 이 경로는 켜는 방향 하나예요. */
export function promoteDomainPolicy(
  ruleId: string,
  acknowledgeIneffective: boolean,
): Promise<DomainPolicyRule> {
  return request<DomainPolicyRule>(
    `${BASE}/${encodeURIComponent(ruleId)}/promote`,
    {
      method: "POST",
      headers: JSON_HEADERS,
      body: JSON.stringify({ acknowledge_ineffective: acknowledgeIneffective }),
    },
  );
}

export function refreshDomainPolicy(ruleId: string): Promise<DomainPolicyRule> {
  return request<DomainPolicyRule>(
    `${BASE}/${encodeURIComponent(ruleId)}/refresh`,
    { method: "POST" },
  );
}

export function deleteDomainPolicy(
  ruleId: string,
): Promise<{ deleted: boolean; reason: string }> {
  return request<{ deleted: boolean; reason: string }>(
    `${BASE}/${encodeURIComponent(ruleId)}`,
    { method: "DELETE" },
  );
}

// ── 화면 문구를 만드는 순수 함수 ──────────────────────────────────────────

/**
 * MCP 도구에 선언된 변수명과 100% 일치할 때만 동작해요. 불일치하면 요청이 거부되지 않아요.
 *
 * 이 문구를 접어두지 않아요. `has` 가드가 fail-closed 를 주는 대가로, 인자 이름이 1글자만
 * 달라도 그 permit 은 아무 요청에도 안 맞고 — 그러면 다른 permit 이 요청을 그대로 통과시켜요.
 * 즉 오타는 「거부」가 아니라 「규칙 없음」이에요.
 */
export const ARGUMENT_NAME_WARNING =
  "MCP 도구에 선언된 변수명과 100% 일치할 때만 동작해요. 불일치하면 요청이 거부되지 않아요.";

export type EnforcementEffect =
  | "observing_no_impact"
  | "observing_tool_closed"
  | "enforcing"
  | "enforcing_shadowed"
  | "unknown";

/**
 * 「이 정책이 지금 무슨 일을 하나」. 강제 모드 하나만 보면 틀려요.
 *
 * `permit` 의 LOG_ONLY 는 「막지 않음」이 아니라 「허용하지 않음」이에요. 굵은 문이 그 action 을
 * 허용하는 동안에는 아무 영향이 없고, 굵은 문에서 빠진 뒤에는 승격 전까지 그 도구가
 * default-deny 로 막혀요. `coarsePermitsAction === null` 은 관측 실패라 `unknown` 이에요.
 *
 * 서버의 `domain_policy.enforcement_effect` 와 같은 표를 구현해요.
 */
export function enforcementEffect(
  mode: EnforcementMode | string,
  coarsePermitsAction: boolean | null,
): EnforcementEffect {
  if (coarsePermitsAction === null) return "unknown";
  if (mode === "ACTIVE") {
    return coarsePermitsAction ? "enforcing_shadowed" : "enforcing";
  }
  if (mode === "LOG_ONLY") {
    return coarsePermitsAction ? "observing_no_impact" : "observing_tool_closed";
  }
  return "unknown";
}

export interface EffectCopy {
  headline: string;
  body: string;
  tone: "info" | "warn" | "ok" | "muted";
}

export const EFFECT_COPY: Record<EnforcementEffect, EffectCopy> = {
  observing_no_impact: {
    headline: "지금은 막지 않아요 — 관측만 해요",
    body:
      "이 정책은 LOG_ONLY 예요. 그리고 다른 ACTIVE permit 이 이 도구를 이미 허용하고 있어서, " +
      "승격해도 요청이 거부되지 않아요. 실제로 막으려면 굵은 문에서 이 도구를 먼저 빼야 해요.",
    tone: "info",
  },
  observing_tool_closed: {
    headline: "⚠️ 승격하기 전까지 이 도구는 막혀 있어요",
    body:
      "이 정책은 LOG_ONLY 라 아무것도 허용하지 않아요. 그런데 이 도구를 허용하는 다른 ACTIVE " +
      "permit 도 없어요 — Cedar 는 기본이 거부라서, 지금 이 도구 호출은 전부 거부돼요. " +
      "임계값을 확인한 뒤 ACTIVE 로 승격해 주세요.",
    tone: "warn",
  },
  enforcing: {
    headline: "강제되고 있어요",
    body: "임계값을 만족하는 호출만 Cedar 를 통과해요.",
    tone: "ok",
  },
  enforcing_shadowed: {
    headline: "ACTIVE 인데 효력이 없어요",
    body:
      "다른 ACTIVE permit 이 이 도구를 이미 허용해요. Cedar 의 permit 은 합집합이라, 좁은 " +
      "정책을 추가해도 권한이 좁아지지 않아요. 굵은 문에서 이 도구를 빼야 이 규칙이 살아나요.",
    tone: "warn",
  },
  unknown: {
    headline: "지금 무슨 일을 하는지 확인하지 못했어요",
    body:
      "강제 모드나 굵은 문 상태를 관측하지 못했어요. 관측하지 못한 것을 「영향 없음」으로 " +
      "읽지 마세요 — 새로고침으로 다시 확인해 주세요.",
    tone: "muted",
  },
};

/**
 * 원장 행에서 「굵은 문이 이 action 을 허용하나」를 읽어요.
 *
 * 생성·승격 시점에 관측한 값이에요. 라이브를 지금 다시 본 게 아니라서, 화면은 이걸
 * 「그때 관측」으로 표시해야 해요.
 */
export function coarsePermitsFromRule(rule: DomainPolicyRule): boolean {
  return rule.coarse_conflicts.length > 0;
}

/** 사람이 읽는 규칙 한 줄. 예: `amount <= 100000` */
export function ruleExpression(rule: {
  argument: string;
  operator: string;
  value_kind: string;
  threshold: string;
}): string {
  const value =
    rule.value_kind === "string" ? JSON.stringify(rule.threshold) : rule.threshold;
  return `${rule.argument} ${rule.operator} ${value}`;
}

/** 이 도구에서 고를 수 있는 인자만. 스키마를 못 읽었으면 빈 배열이에요(= 자유 입력). */
export function selectableArguments(tool: ToolOption | undefined): ToolArgument[] {
  if (!tool) return [];
  return tool.arguments.filter((argument) => argument.usable);
}

/** 자유 입력을 허용해야 하는 상황인지 — 스키마를 못 읽었거나 고를 인자가 없을 때예요. */
export function needsFreeTextArgument(tool: ToolOption | undefined): boolean {
  if (!tool) return false;
  return !tool.schema_observed || selectableArguments(tool).length === 0;
}
