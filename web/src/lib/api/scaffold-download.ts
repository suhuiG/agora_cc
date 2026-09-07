export function scaffoldDownloadPath(includeDevIdentity: boolean): string {
  const params = new URLSearchParams({ format: "zip" });
  if (includeDevIdentity) params.set("include_dev_identity", "true");
  return `/api/playground/scaffold?${params.toString()}`;
}

/** 백엔드가 제외된 operation id 를 실어 보내는 헤더 (`router._EXCLUDED_OPERATIONS_HEADER`). */
export const EXCLUDED_OPERATIONS_HEADER = "X-Agora-Excluded-Operations";

/**
 * 제외된 operation id 목록. 없으면 빈 배열.
 *
 * ## 왜 헤더인가
 *
 * 성공 응답 본문은 ZIP 바이너리라(`res.blob()`) JSON 필드를 붙일 자리가 없어요. 알림을 위해
 * 요청을 하나 더 보내면 두 응답이 어긋날 수 있고, ZIP 안에 넣으면 브라우저가 풀 수 없어서
 * 알림을 띄울 수 없어요. 그래서 같은 응답의 헤더로 받아요.
 *
 * 값은 ASCII JSON 배열이에요 — HTTP 헤더는 latin-1 이라 한국어 사유를 실을 수 없어요.
 * **안내 문구는 화면이 만들어요.** 형식이 깨져 있으면 조용히 빈 배열로 떨어뜨려요: 알림 하나
 * 때문에 이미 성공한 다운로드를 실패로 만들면 안 되니까요.
 */
export function excludedOperations(response: Response): string[] {
  const raw = response.headers.get(EXCLUDED_OPERATIONS_HEADER);
  if (!raw) return [];
  try {
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed.filter((item): item is string => (
      typeof item === "string" && item.length > 0
    ));
  } catch {
    return [];
  }
}

export type ScaffoldResponseError = {
  message: string;
  detail?: {
    message?: string;
    code?: string;
    action_url?: string;
  };
};

export async function scaffoldResponseError(
  response: Response,
): Promise<ScaffoldResponseError> {
  try {
    const body = (await response.json()) as { detail?: unknown };
    if (typeof body.detail === "string" && body.detail.trim()) {
      return { message: body.detail };
    }
    if (
      body.detail
      && typeof body.detail === "object"
      && "message" in body.detail
      && typeof body.detail.message === "string"
      && body.detail.message.trim()
    ) {
      const detail = body.detail as Record<string, unknown>;
      return {
        message: body.detail.message,
        detail: {
          message: body.detail.message,
          code: typeof detail.code === "string" ? detail.code : undefined,
          action_url: typeof detail.action_url === "string"
            ? detail.action_url
            : undefined,
        },
      };
    }
  } catch {
    // Non-JSON upstream errors fall back to the HTTP status text.
  }
  return {
    message: response.statusText || "스캐폴드 생성에 실패했어요.",
  };
}

export async function scaffoldResponseErrorMessage(
  response: Response,
): Promise<string> {
  return (await scaffoldResponseError(response)).message;
}

export function scaffoldDownloadFailureMessage(error: unknown): string {
  return error instanceof Error && error.message.trim()
    ? error.message
    : "스캐폴드 생성에 실패했어요. 잠시 후 다시 시도해 주세요.";
}
