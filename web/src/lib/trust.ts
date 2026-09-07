import type { ApprovalBlock, TrustSummary } from "./api/types.ts";

export type TrustTone = "neutral" | "progress" | "success" | "warning" | "danger";

export const TONE_CLASSES: Record<TrustTone, string> = {
  neutral: "border-slate-300 bg-slate-100 text-slate-700",
  progress: "border-blue-300 bg-blue-50 text-blue-800",
  success: "border-emerald-300 bg-emerald-50 text-emerald-800",
  warning: "border-amber-300 bg-amber-50 text-amber-900",
  danger: "border-red-300 bg-red-50 text-red-800",
};

export type TrustPresentation = {
  statusLabel: string;
  riskLabel: string | null;
  tierLabel: string;
  tone: TrustTone;
  animated: boolean;
  ariaLabel: string;
};

export type OverlapPresentation = {
  statusLabel: string;
  tone: TrustTone;
  animated: boolean;
  ariaLabel: string;
};

export type OverlapSummary = Pick<
  TrustSummary,
  "overlap_state" | "overlap_count" | "overlap_band"
>;

const TIER_LABELS: Record<string, string> = {
  minimal: "최소",
  standard: "표준",
  strong: "강화",
};

const RISK_LABELS: Record<string, string> = {
  none: "위험 신호 없음",
  low: "낮은 위험",
  medium: "중간 위험",
  high: "높은 위험",
};

const VERDICT_LABELS: Record<string, string> = {
  none: "자동 판정 없음",
  pending: "판정 대기",
  "auto-approve": "자동 승인",
  "auto-reject": "자동 반려",
  approved: "승인",
  "approved-override": "예외 승인",
  rejected: "반려",
  deprecated: "폐기",
};

function completedTone(verdict: string | null, risk: string | null): TrustTone {
  if (risk === "high" || verdict === "auto-reject" || verdict === "rejected") {
    return "danger";
  }
  if (
    risk === "low" ||
    risk === "medium" ||
    verdict === "approved-override" ||
    verdict === "pending" ||
    verdict === null
  ) {
    return "warning";
  }
  if (risk === "none" && (verdict === "approved" || verdict === "auto-approve")) {
    return "success";
  }
  return "neutral";
}

export function trustPresentation(
  trust: TrustSummary | null | undefined,
): TrustPresentation | null {
  if (!trust) return null;

  const tierLabel = TIER_LABELS[trust.tier] ?? trust.tier;
  let statusLabel: string;
  let riskLabel: string | null = null;
  let tone: TrustTone;
  let animated = false;

  switch (trust.scan_state) {
    case "not_scanned":
      statusLabel = "미스캔";
      tone = "warning";
      break;
    case "scanning":
      statusLabel = "스캔 중";
      tone = "progress";
      animated = true;
      break;
    case "failed":
      statusLabel = "스캔 실패";
      tone = "danger";
      break;
    case "exempt":
      // 스캔 면제 — 플랫폼이 만든 표준 scaffold(Initializr 자동 생성 agent)라 보안 스캔을
      // 돌리지 않고 자동 승인했어요. `미검토`와 달리 정당화된 상태라 중립 톤으로 명시해요.
      statusLabel = "보안 스캔 면제";
      riskLabel = "Initializr 자동 생성";
      tone = "neutral";
      break;
    case "scanned":
      statusLabel = trust.verdict
        ? (VERDICT_LABELS[trust.verdict] ?? `판정: ${trust.verdict}`)
        : "판정 대기";
      riskLabel = trust.scan_risk
        ? (RISK_LABELS[trust.scan_risk] ?? `위험도: ${trust.scan_risk}`)
        : "위험도 정보 없음";
      tone = completedTone(trust.verdict, trust.scan_risk);
      break;
  }

  const ariaParts = [
    `신뢰 정보: ${statusLabel}`,
    riskLabel,
    `보안 등급 ${tierLabel}`,
  ].filter((part): part is string => Boolean(part));

  return {
    statusLabel,
    riskLabel,
    tierLabel,
    tone,
    animated,
    ariaLabel: ariaParts.join(", "),
  };
}

/** 자동승인 차단 사유의 화면 표현. `audience`에 따라 "할 일" 문구가 달라요. */
export type ApprovalBlockPresentation = {
  /** 짧은 사유 제목. 목록·배지에 써요. */
  title: string;
  /** 서버가 관측한 사실(무엇이 비었는지·어느 단계가 미완인지). */
  detail: string;
  /** 이 화면을 보는 사람이 할 수 있는 행동. */
  action: string;
  tone: TrustTone;
  ariaLabel: string;
};

export type ApprovalBlockAudience = "owner" | "admin";

const APPROVAL_BLOCK_TITLES: Record<ApprovalBlock["reason"], string> = {
  responsibility_incomplete: "담당자 연락처 미비",
  responsibility_unobservable: "담당자 정보 확인 실패",
  gate_pending: "게이트 판정 대기",
  gate_rejected: "필수 게이트 미통과",
  status_transition_failed: "상태 반영 실패",
  block_unobservable: "차단 사유 확인 불가",
};

const APPROVAL_BLOCK_TONES: Record<ApprovalBlock["reason"], TrustTone> = {
  responsibility_incomplete: "warning",
  // 관측 실패는 "값이 비었다"보다 심각해요 — 무엇이 필요한지조차 모르는 상태예요.
  responsibility_unobservable: "danger",
  gate_pending: "progress",
  gate_rejected: "danger",
  status_transition_failed: "danger",
  // store 읽기 실패 — 차단 여부 자체를 알 수 없는 관측 붕괴예요.
  block_unobservable: "danger",
};

// audience별 "할 일". 등록자는 자기가 고칠 수 있는 것만, 관리자는 큐에서 할 수 있는 것을 봐요.
const APPROVAL_BLOCK_ACTIONS: Record<
  ApprovalBlock["reason"],
  Record<ApprovalBlockAudience, string>
> = {
  responsibility_incomplete: {
    owner: "자산 상세의 담당자 정보에서 담당자·에스컬레이션 연락처를 채워주세요. 채우면 자동으로 다시 심사돼요.",
    admin: "등록자에게 담당자·에스컬레이션 연락처 입력을 요청하세요. 급하면 사유를 남기고 직접 승인할 수 있어요.",
  },
  responsibility_unobservable: {
    owner: "담당자 정보를 조회하지 못했어요 — 연락처가 비어 있다는 뜻은 아니에요. 잠시 후 다시 확인해 주세요.",
    admin: "담당자 정보 조회 자체가 실패했어요(미비와 다른 상태). 스토어 상태를 확인한 뒤 재스캔하세요.",
  },
  gate_pending: {
    owner: "보안 심사가 아직 끝나지 않았어요. 결과가 나오면 자동으로 다음 단계로 넘어가요.",
    admin: "미완 단계를 재시도하거나, 경고 게이트 미통과는 사유를 남기고 직접 판정하세요.",
  },
  gate_rejected: {
    owner: "위협리포트의 지적사항을 고친 새 버전을 올려주세요.",
    admin: "검출 위협을 확인해 반려를 유지하거나, 사유를 남기고 예외 승인하세요.",
  },
  status_transition_failed: {
    owner: "심사 결과를 반영하는 중 오류가 있었어요. 잠시 후 자동으로 다시 시도해요.",
    admin: "판정은 났지만 Registry 상태 전이가 실패했어요. 재시도로 풀리지 않으면 직접 판정하세요.",
  },
  block_unobservable: {
    owner: "차단 사유를 지금 확인할 수 없어요. 잠시 후 다시 확인해 주세요.",
    admin: "원장 조회가 실패했어요 — 스토어 상태를 확인하세요.",
  },
};

/**
 * 자동승인이 막힌 사유를 화면 문구로. 사유가 없으면 `null`이에요.
 *
 * `null`은 "막힌 기록 없음"일 뿐이라 호출부가 이걸 승인으로 읽으면 안 돼요 — 승인 여부는
 * `trustPresentation`의 verdict가 정본이에요.
 */
export function approvalBlockPresentation(
  block: ApprovalBlock | null | undefined,
  audience: ApprovalBlockAudience = "owner",
): ApprovalBlockPresentation | null {
  if (!block) return null;
  const title = APPROVAL_BLOCK_TITLES[block.reason] ?? "자동 승인 보류";
  const tone = APPROVAL_BLOCK_TONES[block.reason] ?? "warning";
  const action =
    APPROVAL_BLOCK_ACTIONS[block.reason]?.[audience] ?? block.remediation ?? "";
  const detail = block.detail ?? "";
  return {
    title,
    detail,
    action,
    tone,
    ariaLabel: [`자동 승인 보류: ${title}`, detail, action]
      .filter(Boolean)
      .join(", "),
  };
}

export function overlapPresentation(
  trust: OverlapSummary | null | undefined,
): OverlapPresentation | null {
  if (!trust) return null;

  const state = trust.overlap_state ?? "not_reviewed";
  const count = Number.isFinite(trust.overlap_count)
    ? Math.max(0, trust.overlap_count ?? 0)
    : 0;
  let statusLabel: string;
  let tone: TrustTone;
  let animated = false;

  switch (state) {
    case "reviewing":
      statusLabel = "중복검토 중";
      tone = "progress";
      animated = true;
      break;
    case "reviewed":
      if (count === 0) {
        statusLabel = "중복 없음";
        tone = "success";
      } else {
        statusLabel = `중복 후보 ${count}건`;
        tone =
          trust.overlap_band === "high"
            ? "danger"
            : trust.overlap_band === "medium"
              ? "warning"
              : "neutral";
      }
      break;
    case "failed":
      statusLabel = "중복검토 실패";
      tone = "danger";
      break;
    case "not_reviewed":
    default:
      statusLabel = "중복검토 안 함";
      tone = "warning";
      break;
  }

  return {
    statusLabel,
    tone,
    animated,
    ariaLabel: `중복검토: ${statusLabel}`,
  };
}
