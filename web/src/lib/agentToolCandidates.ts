import type {
  AgentToolBinding,
  AgentToolCandidateAsset,
  AgentToolCandidateOperation,
  AgentToolCandidates,
  AgentToolProposal,
} from "./api/identity";

export function candidateUnavailableReason(
  asset: AgentToolCandidateAsset,
): string {
  if (!asset.found) return "MCP 자산을 찾을 수 없어요.";
  if (!asset.approved) return "MCP 승인이 먼저 필요해요.";
  if (!asset.gatewayConnected) return "MCP Gateway 연결이 먼저 필요해요.";
  return "";
}

export function candidateOperationListPresentation(
  asset: AgentToolCandidateAsset,
): {
  state: "missing" | "unknown" | "empty" | "operations";
  message: string;
} {
  if (!asset.found) {
    return {
      state: "missing",
      message: "의존성 자산을 복구한 뒤 다시 확인해 주세요.",
    };
  }
  if (asset.sensitivityStatus === "unknown") {
    const reason = asset.sensitivityReason?.trim() || "미확인";
    return {
      state: "unknown",
      message: `Operation sensitivity를 관측하지 못했어요. 이유: ${reason}`,
    };
  }
  if (asset.operations.length === 0) {
    return {
      state: "empty",
      message: "나열할 operation이 없어요.",
    };
  }
  return { state: "operations", message: "" };
}

export function readyToolProposals(
  candidates: AgentToolCandidates,
  requestJustifications: Record<string, string> = {},
): AgentToolProposal[] {
  return candidates.mcpAssets.flatMap((asset) =>
    asset.operations
      .filter((operation) => operation.ready)
      .map((operation) => ({
        asset_id: asset.assetId,
        asset_version: asset.version,
        operation_id: operation.operationId,
        request_justification:
          requestJustifications[
            `${asset.assetId}:${asset.version}:${operation.operationId}`
          ] ?? "",
      })),
  );
}

export function requestJustificationLabel(value: string | undefined): string {
  return value?.trim() || "사유 없음(이전 등록)";
}

export function findToolCandidateBinding(
  asset: AgentToolCandidateAsset,
  operation: AgentToolCandidateOperation,
  bindings: AgentToolBinding[],
): AgentToolBinding | undefined {
  const matchingOperation = bindings.filter(
    (binding) =>
      binding.asset_id === asset.assetId &&
      binding.operation_id === operation.operationId,
  );

  return (
    matchingOperation.find(
      (binding) => binding.asset_version === asset.version,
    ) ??
    (matchingOperation.length === 1 &&
    (!matchingOperation[0].asset_version || !asset.version)
      ? matchingOperation[0]
      : undefined)
  );
}
