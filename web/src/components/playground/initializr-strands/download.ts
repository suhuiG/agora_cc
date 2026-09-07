import type { ScaffoldDownload, ScaffoldSpecInput } from "@/lib/api/playground";

type ScaffoldDownloader = (
  spec: ScaffoldSpecInput,
  includeDevIdentity: boolean,
) => Promise<ScaffoldDownload>;

export type ScaffoldDownloadAttempt =
  | {
      kind: "downloaded";
      blob: Blob;
      includesDevIdentity: boolean;
      /** 크리덴셜 천장(READ) 밖이라 ZIP 에 안 담긴 operation id. 알림으로 드러내야 해요. */
      excludedOperations: string[];
    }
  | {
      kind: "credential_unavailable";
      reason: string;
      actionUrl?: string;
    };

function errorMessage(error: unknown): string {
  return error instanceof Error && error.message.trim()
    ? error.message
    : "dev 크리덴셜을 발급하지 못했어요.";
}

function errorActionUrl(error: unknown): string | undefined {
  if (!error || typeof error !== "object" || !("detail" in error)) {
    return undefined;
  }
  const detail = error.detail;
  if (!detail || typeof detail !== "object" || !("action_url" in detail)) {
    return undefined;
  }
  const actionUrl = detail.action_url;
  return typeof actionUrl === "string" && actionUrl.startsWith("/")
    ? actionUrl
    : undefined;
}

function isCredentialIssuanceFailure(error: unknown): boolean {
  if (!error || typeof error !== "object" || !("detail" in error)) return false;
  const detail = error.detail;
  if (!detail || typeof detail !== "object" || !("code" in detail)) return false;
  return (
    typeof detail.code === "string"
    && detail.code.startsWith("DEV_IDENTITY_")
  );
}

export async function attemptScaffoldDownload({
  spec,
  includeDevIdentity,
  download,
}: {
  spec: ScaffoldSpecInput;
  includeDevIdentity: boolean;
  download: ScaffoldDownloader;
}): Promise<ScaffoldDownloadAttempt> {
  try {
    const result = await download(spec, includeDevIdentity);
    return {
      kind: "downloaded",
      blob: result.blob,
      includesDevIdentity: includeDevIdentity,
      excludedOperations: result.excludedOperations,
    };
  } catch (error) {
    if (!includeDevIdentity || !isCredentialIssuanceFailure(error)) throw error;
    return {
      kind: "credential_unavailable",
      reason: errorMessage(error),
      actionUrl: errorActionUrl(error),
    };
  }
}
