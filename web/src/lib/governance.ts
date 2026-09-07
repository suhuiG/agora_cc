// 거버넌스 콘솔 UI 상수 (단일 출처).
// 백엔드 seed.py 의 area 키·라이선스 화이트리스트·실행형태·등급과 1:1 매핑돼요.
// 화면설계 §4-A 의 한글 라벨을 여기서 관리해요 (백엔드는 영문 키만 저장).

/** finding 영역 12종 — 백엔드 GovTool.area 키 ↔ 화면설계 §4-A 한글 라벨. */
export const AREA_OPTIONS: { value: string; label: string }[] = [
  { value: "secret", label: "시크릿 스캔" },
  { value: "sast", label: "SAST/악성패턴" },
  { value: "mcp_poison", label: "MCP tool poisoning" },
  { value: "sbom_cve", label: "SBOM+CVE" },
  { value: "signing", label: "코드서명" },
  { value: "pii", label: "PII 정적스캔" },
  { value: "llm_redteam", label: "LLM 레드팀" },
  { value: "license", label: "라이선스 검사" },
  { value: "iac", label: "IaC 보안" },
  { value: "container", label: "컨테이너 스캔" },
  { value: "dependency", label: "의존성 감사" },
  { value: "custom", label: "커스텀" },
];

/** 라이선스 화이트리스트 — 백엔드 models.ALLOWED_LICENSES 와 동일. */
export const ALLOWED_LICENSES = [
  "MIT",
  "Apache-2.0",
  "LGPL-2.1",
  "LGPL-3.0",
  "BSD-3-Clause",
] as const;

/** 실행형태 — 백엔드 GovTool.exec_kind. */
export const EXEC_KIND_OPTIONS: { value: string; label: string }[] = [
  { value: "offline", label: "오프라인 바이너리" },
  { value: "cli", label: "CLI" },
  { value: "service", label: "서비스(REST)" },
];

/** 대상 자산 타입 — 백엔드 GovTool.target_asset_types. */
export const ASSET_TYPE_OPTIONS: { value: string; label: string }[] = [
  { value: "mcp", label: "MCP" },
  { value: "agent", label: "agent" },
  { value: "skill", label: "skill" },
];

/** 보안 등급 3종 (최소/표준/강화) — 백엔드 TierCell.tier 키. */
export const TIERS: { value: string; label: string }[] = [
  { value: "minimal", label: "최소" },
  { value: "standard", label: "표준" },
  { value: "strong", label: "강화" },
];

/** 등급 셀 enforcement 3종 — 백엔드 TierCell.enforcement. */
export const ENFORCEMENT_OPTIONS: { value: string; label: string }[] = [
  { value: "off", label: "—" },
  { value: "warn", label: "경고" },
  { value: "required", label: "필수" },
];

const AREA_LABELS = new Map(AREA_OPTIONS.map((a) => [a.value, a.label]));
const EXEC_LABELS = new Map(EXEC_KIND_OPTIONS.map((e) => [e.value, e.label]));
const ASSET_TYPE_LABELS = new Map(ASSET_TYPE_OPTIONS.map((t) => [t.value, t.label]));

/** area 키 → 한글 라벨 (미등록 키는 그대로 노출). */
export function areaLabel(area: string): string {
  return AREA_LABELS.get(area) ?? area;
}

/** exec_kind 키 → 한글 라벨. */
export function execKindLabel(kind: string): string {
  return EXEC_LABELS.get(kind) ?? kind;
}

/** 대상 자산 타입 키 → 라벨 (mcp→MCP 등, 미등록 키는 그대로). */
export function assetTypeLabel(assetType: string): string {
  return ASSET_TYPE_LABELS.get(assetType) ?? assetType;
}

/** 라이선스 허용 여부 (프론트 사전 검증 — 서버도 422 로 재검증). */
export function isLicenseAllowed(lic: string): boolean {
  return (ALLOWED_LICENSES as readonly string[]).includes(lic);
}

/**
 * 이름 → slug 미리보기 (공백→하이픈, 소문자, 영숫자·하이픈만).
 * 백엔드 models.slugify 와 동일 규칙 — 등록 전 tool_id 를 미리 보여주려고 재현했어요.
 * (커밋 7f1cf9b 교훈: 공백 이름이 AWS 리소스명에서 깨지는 이슈 방지.)
 */
export function slugifyPreview(name: string): string {
  return name
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
}

/** status → 한글 라벨. */
export function statusLabel(status: string): string {
  return status === "active" ? "활성" : status === "staged" ? "준비" : status;
}

const TIER_LABELS = new Map(TIERS.map((t) => [t.value, t.label]));

/** tier 키 → 한글 라벨 (minimal→최소 등). */
export function tierLabel(tier: string): string {
  return TIER_LABELS.get(tier) ?? tier;
}

/** enforcement → 한글 라벨. */
export function enforcementLabel(enf: string): string {
  return { required: "필수", warn: "경고", off: "—" }[enf] ?? enf;
}

/** 게이트 단계 상태 4종 → 아이콘·라벨·색상 (화면설계 §3: ○◐✓✗). */
export const GATE_STATE_META: Record<string, { icon: string; label: string; cls: string }> = {
  pending: { icon: "○", label: "대기중", cls: "text-muted-foreground" },
  running: { icon: "◐", label: "진행중", cls: "text-blue-500" },
  pass: { icon: "✓", label: "PASS", cls: "text-emerald-600" },
  fail: { icon: "✗", label: "FAIL", cls: "text-red-600" },
  // 스캔은 됐지만 이 도구는 scan-runner에 없어 실제 검사되지 않음(통과 아님).
  not_run: { icon: "⊘", label: "미실행", cls: "text-slate-400" },
  // 이 자산엔 이 도구의 검사 대상이 아예 없음(소스 없는 자산의 소스 기반 도구). 통과도 실패도 아님.
  not_applicable: { icon: "—", label: "해당 없음", cls: "text-slate-400" },
  // 적용 여부를 관측하지 못함. 통과가 아니며 자동승인을 막아요.
  unknown: { icon: "?", label: "미해석", cls: "text-amber-700" },
};

/** 위험도 → 색상 배지 클래스. */
export function riskBadgeClass(risk: string | null): string {
  switch (risk) {
    case "high": return "bg-red-100 text-red-800";
    case "medium": return "bg-amber-100 text-amber-800";
    case "low": return "bg-yellow-100 text-yellow-800";
    case "none": return "bg-emerald-100 text-emerald-700";
    default: return "bg-slate-100 text-slate-500";  // 미스캔(null)
  }
}

/** 진행상태 verdict → 한글 라벨. */
export function verdictLabel(verdict: string, scanned: boolean): string {
  if (!scanned && verdict === "pending") return "미스캔";
  return {
    none: "미설정",
    pending: "검토중",
    "auto-approve": "자동 APPROVE",
    "auto-reject": "자동 REJECT",
    // 사람의 최종 결정(effective_verdict) — 스캔 게이트보다 우선 표시.
    approved: "승인",
    "approved-override": "승인됨 (override)",
    rejected: "반려",
    deprecated: "폐기",
  }[verdict] ?? verdict;
}

/** 진행상태 verdict → 텍스트 색상 클래스. 사람 결정/자동 판정을 색으로 구분해요. */
export function verdictClass(verdict: string): string {
  switch (verdict) {
    case "approved":
    case "approved-override": return "text-emerald-700";   // 승인(override 포함)
    case "auto-approve": return "text-blue-600";
    case "rejected": return "text-red-700";
    case "auto-reject": return "text-amber-600";           // 자동 reject(사람 결정 전) — 경고색
    case "deprecated": return "text-slate-400";
    default: return "text-foreground";                     // pending/none/미스캔
  }
}

/** compute 키 → 라벨. */
export function computeLabel(compute: string): string {
  return { lambda: "Lambda", fargate: "Fargate" }[compute] ?? compute;
}

/**
 * registry 라이프사이클 status → 한글 라벨.
 * 백엔드 값: DRAFT | PENDING_APPROVAL | APPROVED | REJECTED | DEPRECATED.
 * (governance.ts 상단 statusLabel(active/staged)과는 다른 축이라 별도 함수예요.)
 */
export function registryStatusLabel(status: string): string {
  return {
    DRAFT: "초안",
    PENDING_APPROVAL: "심사중",
    APPROVED: "승인",
    REJECTED: "반려",
    DEPRECATED: "폐기",
  }[status] ?? status;   // 미등록 값은 원문 그대로 노출
}

/**
 * registry 라이프사이클 status → 색상 배지 클래스.
 * riskBadgeClass 의 색 관례를 따라요(빨강/노랑/초록/회색). 새 색 시스템은 만들지 않아요.
 */
export function registryStatusBadgeClass(status: string): string {
  switch (status) {
    case "DRAFT": return "bg-slate-100 text-slate-600";       // 회색(초안)
    case "PENDING_APPROVAL": return "bg-amber-100 text-amber-800"; // 노랑(심사중)
    case "APPROVED": return "bg-emerald-100 text-emerald-700"; // 초록(승인)
    case "REJECTED": return "bg-red-100 text-red-800";         // 빨강(반려)
    case "DEPRECATED": return "bg-slate-100 text-slate-500";   // 뮤트(폐기)
    default: return "bg-slate-100 text-slate-500";             // 미등록 값도 뮤트
  }
}
