// 브라우저는 백엔드에 직접 접근하지 않아요. 동일 출처 BFF가 HttpOnly 세션을
// Cognito access token으로 교환하고 신뢰할 수 없는 identity 헤더를 제거해 전달해요.
export const API_BASE = "/api/backend";

// 이전 함수 시그니처와 호출부를 유지하기 위한 값이에요. BFF가 이 값을 전송하지 않으며,
// 백엔드의 검증된 Cognito principal만 owner/audit 주체가 됩니다.
export const SESSION_PRINCIPAL = "cognito-session";

// ---------------------------------------------------------------------------
// 공통 에러 / 요청 헬퍼
// ---------------------------------------------------------------------------

/** 구조화된 에러 detail. 백엔드가 복구 방법을 함께 내려줄 때 쓰는 형태예요. */
export type ApiErrorDetail = {
  message?: string;
  reason?: string;
  record_id?: string;
  remediation?: string;
  code?: string;
  action_url?: string;
  change?: unknown;
};

/** HTTP 상태 코드를 함께 들고 다니는 API 에러예요. (마법사에서 status 별 분기 가능) */
export class ApiError extends Error {
  status: number;
  /** 백엔드가 객체 detail을 준 경우 원본을 보존해요. 문자열 detail이면 undefined. */
  detail?: ApiErrorDetail;
  /** 백엔드 응답의 `detail` 원형. 화면이 서버 관측값과 로컬 폴백을 구분할 때 써요. */
  responseDetail?: unknown;
  constructor(
    message: string,
    status: number,
    detail?: ApiErrorDetail,
    responseDetail?: unknown,
  ) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
    this.responseDetail = responseDetail;
  }
}

/** 서버 `detail`에서 사람이 읽을 수 있는 사유만 꺼내요. 값은 고치지 않고 그대로 돌려줘요. */
export function apiErrorServerReason(error: ApiError): string | undefined {
  const raw = error.responseDetail;
  if (typeof raw === "string") {
    return raw.trim() ? raw : undefined;
  }
  if (Array.isArray(raw)) {
    const messages = raw.flatMap((item) => {
      if (!item || typeof item !== "object") return [];
      const message = (item as Record<string, unknown>).msg;
      return typeof message === "string" && message.trim() ? [message] : [];
    });
    return messages.length > 0 ? messages.join("\n") : undefined;
  }
  if (raw && typeof raw === "object") {
    const detail = raw as Record<string, unknown>;
    for (const key of ["message", "reason", "remediation"]) {
      const value = detail[key];
      if (typeof value === "string" && value.trim()) return value;
    }
  }
  return undefined;
}

/**
 * FastAPI `detail`을 사람이 읽을 메시지와 구조화 detail로 정규화해요.
 *
 * FastAPI는 `HTTPException(status, dict)`을 `{"detail": {...}}`로 직렬화해요. 예전엔
 * detail을 문자열로 단언해서 객체가 오면 `[object Object]`가 화면에 찍혔어요 — 반려 응답의
 * `record_id`·`remediation`처럼 사용자가 스스로 복구하는 데 필요한 정보가 그대로 사라졌어요.
 */
function parseDetail(raw: unknown): { message?: string; detail?: ApiErrorDetail } {
  if (typeof raw === "string") return { message: raw };
  if (raw && typeof raw === "object") {
    const detail = raw as ApiErrorDetail;
    return { message: detail.message, detail };
  }
  return {};
}

/**
 * fetch 래퍼. 응답이 ok 아니면 JSON 바디에서 `detail` 을 꺼내 ApiError 로 던져요.
 * ok 면 JSON 을 `T` 로 파싱해서 돌려줘요.
 */
export async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(API_BASE + path, init);
  if (!res.ok) {
    let message: string | undefined;
    let detail: ApiErrorDetail | undefined;
    let responseDetail: unknown;
    try {
      const body = (await res.json()) as { detail?: unknown };
      responseDetail = body?.detail;
      const parsed = parseDetail(responseDetail);
      message = parsed.message;
      detail = parsed.detail;
    } catch {
      // 바디가 JSON 이 아닐 수도 있어요. 그러면 statusText 로 폴백해요.
    }
    throw new ApiError(
      message || res.statusText,
      res.status,
      detail,
      responseDetail,
    );
  }
  return (await res.json()) as T;
}

export const JSON_HEADERS = { "Content-Type": "application/json" } as const;

/** principal 인자는 이전 호출부 호환용이고, 실제 주체는 BFF 세션에서만 결정해요. */
export function jsonHeadersWithPrincipal(_principal: string): Record<string, string> {
  void _principal;
  return { ...JSON_HEADERS };
}
