import type {
  AuthorizationGateApprovalHistoryItem,
  AuthorizationGateAssessment,
  AuthorizationInventory,
  AuthorizationReclaimCandidate,
} from "./api/authorizationInventory";

export type GatePresentation = {
  label: string;
  detail: string;
  tone: "neutral" | "warning" | "danger";
};

export function gatePresentation(
  gate: AuthorizationGateAssessment,
): GatePresentation {
  if (gate.reason === "ih48_negative_control_unqualified") {
    return {
      label: "도구 0건 · 관측 자격 미달",
      detail:
        "Gateway inventory에서 ACTIVE 도구가 0건입니다. 확인할 호출 대상이 없어 "
        + "IH-48 독립 관측 자격을 확인할 수 없으므로 이 snapshot은 "
        + "승인할 수 없습니다.",
      tone: "warning",
    };
  }
  if (gate.state === "unknown") {
    if (gate.reason === "registry_agent_inventory_incomplete") {
      return {
        label: "배포 Agent 평가 미완",
        detail:
          "인가가 필요한 APPROVED Runtime Agent를 인가 inventory에서 "
          + "평가하지 못했습니다. 평가가 완전하지 않아 승인할 수 없습니다.",
        tone: "warning",
      };
    }
    return {
      label: "관측 불가",
      detail: "현재 상태를 관측하지 못해 게이트를 승인할 수 없습니다.",
      tone: "warning",
    };
  }
  if (gate.state === "not_applicable") {
    return {
      label: "대상 없음",
      detail:
        gate.reason === "authorization_target_agents_empty"
          ? "현재 인가 원장 행이 필요한 APPROVED Runtime Agent가 없습니다. "
            + "대상 0건만으로는 전환을 승인할 수 없습니다."
          : "인가 적용 대상인 Agent가 없습니다.",
      tone: "neutral",
    };
  }

  const affected = gate.affected_agent_count ?? 0;
  const evaluated = gate.evaluated_agent_count ?? 0;
  const impactDetail =
    `평가 대상 ${evaluated.toLocaleString()}건 중 `
    + `${affected.toLocaleString()}건이 authorization_verdict=unknown이며 `
    + "fail-closed 전환 시 새로 차단됩니다.";
  return {
    label: `${affected.toLocaleString()}건 영향`,
    detail: impactDetail,
    tone: affected > 0 ? "danger" : "neutral",
  };
}

export function approvalHistoryPresentation(
  item: AuthorizationGateApprovalHistoryItem,
) {
  if (item.state === "unknown") {
    return {
      label: "승인 근거 판독 불가",
      detail:
        `${item.approved_by} · ${item.approved_at} · `
        + (item.reason || "판독 실패 사유 미확인"),
      tone: "warning" as const,
    };
  }
  return {
    label: "승인 근거 확인됨",
    detail: `${item.approved_by} · ${item.approved_at}`,
    tone: "neutral" as const,
  };
}

export function inventoryDetailPresentation(
  status: AuthorizationInventory["status"],
  reason: string,
) {
  if (status === "observed") return null;
  return {
    title: "상세 데이터를 관측하지 못했어요.",
    detail: reason || "관측 실패 사유 미확인",
  };
}

export function approvalCurrencyLabel(approved: boolean) {
  return approved
    ? "현재 관측과 일치"
    : "만료 · 현재 관측과 다름";
}

export function cleanupCandidatePresentation(
  candidate: AuthorizationReclaimCandidate,
) {
  const resources = [
    candidate.client_id ? `client ${candidate.client_id}` : "",
    candidate.policy_ids.length
      ? `policy ${candidate.policy_ids.length.toLocaleString()}건`
      : "",
  ].filter(Boolean);
  return {
    mode: "report_only" as const,
    title: candidate.record_id,
    detail: resources.join(" · ") || "원장 artifact",
    reason:
      candidate.reason === "catalog_record_missing"
        ? "카탈로그 record 없음"
        : candidate.reason,
  };
}
