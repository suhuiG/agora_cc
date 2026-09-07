import type {
  SensitivitySource as LedgerSensitivitySource,
} from "./api/mcpToolDrift";

export type SensitivitySource =
  | ""
  | LedgerSensitivitySource;

export type SensitivityObservationStatus = "observed" | "unknown";

const SOURCE_UNKNOWN = "이 등급의 출처를 확인하지 못했어요.";

/**
 * 실제 drift 원장이 기록한 태그 출처를 사람이 읽는 공시로 바꿔요.
 *
 * `status=unknown`은 원장을 못 읽은 상태이고, `source=unknown`은 원장은 읽었지만
 * legacy 행에 출처 기록이 없는 상태예요. 둘 다 추정으로 메우지 않지만 데이터 상태는
 * API에서 서로 다르게 보존해요.
 */
export function sensitivitySourceMessage(
  sensitivity: string | undefined,
  source: SensitivitySource | undefined,
  status: SensitivityObservationStatus | undefined,
): string | null {
  if (status !== "observed") return SOURCE_UNKNOWN;
  if (!sensitivity?.trim()) return null;

  switch (source) {
    case "descriptor":
      return "MCP가 직접 선언한 등급이에요.";
    case "name_guess":
      return "플랫폼이 도구 이름에서 짐작한 등급이에요 — MCP 저자가 선언한 값이 아니에요.";
    case "auto":
      return "플랫폼이 등록 시점 자동 분류(LLM 또는 보수적 기본)로 정한 등급이에요 — MCP 저자가 선언한 값이 아니에요.";
    case "admin":
      return "관리자가 확인하고 확정한 등급이에요.";
    case "unknown":
    case "":
    case undefined:
      return SOURCE_UNKNOWN;
  }
}
