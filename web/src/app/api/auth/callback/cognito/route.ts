import { timingSafeEqual } from "node:crypto";
import { NextRequest, NextResponse } from "next/server";
import { authCallbackUrl, getAuthConfig } from "@/lib/auth/config";
import { consumeOAuthState } from "@/lib/auth/session-store";
import {
  cookieSecure,
  createSession,
  OAUTH_STATE_COOKIE,
  setSessionCookie,
} from "@/lib/auth/session";
import { safeReturnTo } from "@/lib/auth/return-to";

type CognitoTokenResponse = {
  access_token?: string;
  expires_in?: number;
  refresh_token?: string;
  token_type?: string;
};

function sameOpaqueValue(left: string, right: string): boolean {
  const leftBuffer = Buffer.from(left, "utf8");
  const rightBuffer = Buffer.from(right, "utf8");
  return (
    leftBuffer.length === rightBuffer.length &&
    timingSafeEqual(leftBuffer, rightBuffer)
  );
}

function clearStateCookie(response: NextResponse): void {
  response.cookies.set({
    name: OAUTH_STATE_COOKIE,
    value: "",
    httpOnly: true,
    maxAge: 0,
    path: "/api/auth/callback/cognito",
    sameSite: "lax",
    secure: cookieSecure(),
  });
}

function loginFailure(config: ReturnType<typeof getAuthConfig>, reason: string): NextResponse {
  const url = new URL("/login", config.webBaseUrl);
  url.searchParams.set("authError", reason);
  const response = NextResponse.redirect(url);
  clearStateCookie(response);
  return response;
}

export async function GET(request: NextRequest) {
  const config = getAuthConfig();
  const state = request.nextUrl.searchParams.get("state") || "";
  const stateCookie = request.cookies.get(OAUTH_STATE_COOKIE)?.value || "";
  const code = request.nextUrl.searchParams.get("code") || "";
  if (
    request.nextUrl.searchParams.has("error") ||
    !state ||
    !stateCookie ||
    !code ||
    !sameOpaqueValue(state, stateCookie)
  ) {
    return loginFailure(config, "invalid_callback");
  }

  const oauthState = await consumeOAuthState(state);
  if (!oauthState) {
    return loginFailure(config, "expired_state");
  }

  const tokenResponse = await fetch(`${config.cognitoDomain}/oauth2/token`, {
    method: "POST",
    body: new URLSearchParams({
      client_id: config.clientId,
      code,
      code_verifier: oauthState.codeVerifier,
      grant_type: "authorization_code",
      redirect_uri: authCallbackUrl(config),
    }),
    cache: "no-store",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
  });
  if (!tokenResponse.ok) {
    return loginFailure(config, "token_exchange_failed");
  }

  try {
    const tokens = (await tokenResponse.json()) as CognitoTokenResponse;
    const { opaqueSessionId } = await createSession(tokens);
    // defense-in-depth: 저장된 returnTo 도 최종 redirect 전에 한 번 더 검증해요(cross-origin 차단).
    const response = NextResponse.redirect(
      new URL(safeReturnTo(oauthState.returnTo), config.webBaseUrl),
    );
    setSessionCookie(response, opaqueSessionId);
    clearStateCookie(response);
    return response;
  } catch {
    return loginFailure(config, "identity_rejected");
  }
}

