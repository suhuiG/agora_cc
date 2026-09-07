import "server-only";

import { randomBytes } from "node:crypto";
import type { NextRequest, NextResponse } from "next/server";
import { getAuthConfig } from "./config";
import {
  displayNameFromProfile,
  normalizeDisplayName,
  type CognitoUserProfile,
} from "./profile";
import {
  deleteSession,
  getSession,
  putSession,
  type SessionRecord,
} from "./session-store";
import { shouldRefreshAccessToken } from "./session-refresh";

export const SESSION_COOKIE = "agora_session";
export const OAUTH_STATE_COOKIE = "agora_oauth_state";

const SESSION_TTL_SECONDS = 8 * 60 * 60;

export type AgoraPrincipal = {
  can: {
    manage_access: boolean;
    manage_governance: boolean;
    register_asset: boolean;
  };
  // 담당자 표시용 email. pre-token Lambda가 access token claim으로 넣어 /api/me가 반환해요.
  // 없는 환경·기존 세션은 빈 문자열이에요.
  email: string;
  principal_id: string;
  roles: string[];
  source: string;
  team: string;
};

type CognitoTokenResponse = {
  access_token?: string;
  expires_in?: number;
  refresh_token?: string;
  token_type?: string;
};

export function opaqueToken(bytes = 32): string {
  return randomBytes(bytes).toString("base64url");
}

export function cookieSecure(): boolean {
  return new URL(getAuthConfig().webBaseUrl).protocol === "https:";
}

export function setSessionCookie(
  response: NextResponse,
  opaqueSessionId: string,
): void {
  response.cookies.set({
    name: SESSION_COOKIE,
    value: opaqueSessionId,
    httpOnly: true,
    maxAge: SESSION_TTL_SECONDS,
    path: "/",
    sameSite: "lax",
    secure: cookieSecure(),
  });
}

export function clearSessionCookie(response: NextResponse): void {
  response.cookies.set({
    name: SESSION_COOKIE,
    value: "",
    httpOnly: true,
    maxAge: 0,
    path: "/",
    sameSite: "lax",
    secure: cookieSecure(),
  });
}

export async function createSession(
  tokenResponse: CognitoTokenResponse,
  options: { displayName?: string } = {},
): Promise<{
  displayName: string;
  opaqueSessionId: string;
  principal: AgoraPrincipal;
}> {
  const accessToken = tokenResponse.access_token;
  const refreshToken = tokenResponse.refresh_token;
  const expiresIn = Number(tokenResponse.expires_in);
  if (
    !accessToken ||
    !refreshToken ||
    tokenResponse.token_type?.toLowerCase() !== "bearer" ||
    !Number.isFinite(expiresIn) ||
    expiresIn <= 0
  ) {
    throw new Error("Cognito returned an incomplete token response");
  }

  const principal = await fetchPrincipal(accessToken);
  const displayName =
    normalizeDisplayName(options.displayName) ??
    (await fetchCognitoDisplayName(accessToken, principal.principal_id)) ??
    principal.principal_id;
  const now = Math.floor(Date.now() / 1000);
  const opaqueSessionId = opaqueToken();
  await putSession(opaqueSessionId, {
    accessToken,
    accessTokenExpiresAt: now + expiresIn,
    displayName,
    expiresAt: now + SESSION_TTL_SECONDS,
    refreshToken,
  });
  return { displayName, opaqueSessionId, principal };
}

export async function authenticatedSession(
  request: NextRequest,
): Promise<{
  accessToken: string;
  displayName: string;
  principal: AgoraPrincipal;
} | null> {
  const opaqueSessionId = request.cookies.get(SESSION_COOKIE)?.value;
  if (!opaqueSessionId) {
    return null;
  }

  const now = Math.floor(Date.now() / 1000);
  let record = await getSession(opaqueSessionId);
  if (!record || record.expiresAt <= now) {
    await deleteSession(opaqueSessionId);
    return null;
  }
  if (shouldRefreshAccessToken(record.accessTokenExpiresAt, now)) {
    record = await refreshSession(opaqueSessionId, record);
    if (!record) {
      return null;
    }
  }

  try {
    const principal = await fetchPrincipal(record.accessToken);
    return {
      accessToken: record.accessToken,
      displayName: normalizeDisplayName(record.displayName) ?? principal.principal_id,
      principal,
    };
  } catch {
    await deleteSession(opaqueSessionId);
    return null;
  }
}

export async function removeRequestSession(request: NextRequest): Promise<void> {
  const opaqueSessionId = request.cookies.get(SESSION_COOKIE)?.value;
  if (opaqueSessionId) {
    await deleteSession(opaqueSessionId);
  }
}

async function refreshSession(
  opaqueSessionId: string,
  current: SessionRecord,
): Promise<SessionRecord | null> {
  const config = getAuthConfig();
  const body = new URLSearchParams({
    client_id: config.clientId,
    grant_type: "refresh_token",
    refresh_token: current.refreshToken,
  });
  const response = await fetch(`${config.cognitoDomain}/oauth2/token`, {
    method: "POST",
    body,
    cache: "no-store",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
  });
  if (!response.ok) {
    await deleteSession(opaqueSessionId);
    return null;
  }

  const tokens = (await response.json()) as CognitoTokenResponse;
  const accessToken = tokens.access_token;
  const expiresIn = Number(tokens.expires_in);
  if (
    !accessToken ||
    tokens.token_type?.toLowerCase() !== "bearer" ||
    !Number.isFinite(expiresIn) ||
    expiresIn <= 0
  ) {
    await deleteSession(opaqueSessionId);
    return null;
  }

  const record: SessionRecord = {
    accessToken,
    accessTokenExpiresAt: Math.floor(Date.now() / 1000) + expiresIn,
    displayName: current.displayName,
    expiresAt: current.expiresAt,
    refreshToken: current.refreshToken,
  };
  await putSession(opaqueSessionId, record);
  return record;
}

async function fetchPrincipal(accessToken: string): Promise<AgoraPrincipal> {
  const { apiUrl } = getAuthConfig();
  const response = await fetch(`${apiUrl}/api/me`, {
    cache: "no-store",
    headers: {
      Accept: "application/json",
      Authorization: `Bearer ${accessToken}`,
    },
  });
  if (!response.ok) {
    throw new Error(`Agora API rejected the Cognito token (${response.status})`);
  }
  return (await response.json()) as AgoraPrincipal;
}

async function fetchCognitoDisplayName(
  accessToken: string,
  expectedSubject: string,
): Promise<string | undefined> {
  const { cognitoDomain } = getAuthConfig();
  try {
    const response = await fetch(`${cognitoDomain}/oauth2/userInfo`, {
      cache: "no-store",
      headers: {
        Accept: "application/json",
        Authorization: `Bearer ${accessToken}`,
      },
    });
    if (!response.ok) {
      return undefined;
    }
    return displayNameFromProfile(
      (await response.json()) as CognitoUserProfile,
      expectedSubject,
    );
  } catch {
    return undefined;
  }
}
