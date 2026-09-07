import { ApiError, apiErrorServerReason } from "./api/client.ts";

const FALLBACK = "Tool binding 상태를 저장하지 못했어요.";
const MISSING_SERVER_REASON = "서버가 사유를 주지 않았어요.";

/**
 * Agent × Tool 변경 실패를 InlineError에 표시할 문구로 바꿔요.
 *
 * 422는 서버가 실제로 관측한 detail만 보여줘요. detail이 없을 때 원인을 추측하면
 * 선언·버전·operation 검증 중 무엇이 실패했는지 화면이 거짓말할 수 있어요.
 */
export function agentToolBindingErrorMessage(error: unknown): string {
  if (error instanceof ApiError && error.status === 422) {
    return apiErrorServerReason(error) ?? MISSING_SERVER_REASON;
  }
  return error instanceof Error ? error.message : FALLBACK;
}
