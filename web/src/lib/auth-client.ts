import { DEFAULT_RETURN_TO, safeReturnTo } from "@/lib/auth/return-to";

export { safeReturnTo } from "@/lib/auth/return-to";

export interface AuthSession {
  authenticated: boolean;
  display_name?: string;
  email?: string;
  principal_id?: string;
  roles?: string[];
  team?: string;
}

function isAuthSession(value: unknown): value is AuthSession {
  if (!value || typeof value !== "object") {
    return false;
  }

  const session = value as Record<string, unknown>;
  return (
    typeof session.authenticated === "boolean" &&
    (session.display_name === undefined || typeof session.display_name === "string") &&
    (session.email === undefined || typeof session.email === "string") &&
    (session.principal_id === undefined || typeof session.principal_id === "string") &&
    (session.team === undefined || typeof session.team === "string") &&
    (session.roles === undefined ||
      (Array.isArray(session.roles) && session.roles.every((role) => typeof role === "string")))
  );
}

export async function getAuthSession(signal?: AbortSignal): Promise<AuthSession> {
  const response = await fetch("/api/auth/session", {
    cache: "no-store",
    credentials: "same-origin",
    headers: { Accept: "application/json" },
    signal,
  });

  if (!response.ok) {
    throw new Error(`세션 확인에 실패했습니다. (${response.status})`);
  }

  const session: unknown = await response.json();
  if (!isAuthSession(session)) {
    throw new Error("세션 응답 형식이 올바르지 않습니다.");
  }

  return session;
}

export async function postSession(tokens: {
  id_token: string;
  access_token: string;
  refresh_token: string;
  expires_in: number;
}): Promise<AuthSession> {
  const response = await fetch("/api/auth/session", {
    method: "POST",
    cache: "no-store",
    credentials: "same-origin",
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
    },
    body: JSON.stringify(tokens),
  });

  if (!response.ok) {
    throw new Error(`세션 생성에 실패했습니다. (${response.status})`);
  }
  const session: unknown = await response.json();
  if (!isAuthSession(session)) {
    throw new Error("세션 응답 형식이 올바르지 않습니다.");
  }
  return session;
}

export function loginUrl(returnTo?: string): string {
  const params = new URLSearchParams({
    returnTo: safeReturnTo(returnTo),
  });
  return `/api/auth/login?${params.toString()}`;
}

export function currentReturnTo(): string {
  if (typeof window === "undefined") {
    return DEFAULT_RETURN_TO;
  }
  return safeReturnTo(`${window.location.pathname}${window.location.search}${window.location.hash}`);
}
