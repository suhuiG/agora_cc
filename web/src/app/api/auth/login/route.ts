import { createHash } from "node:crypto";
import { NextRequest, NextResponse } from "next/server";
import { authCallbackUrl, getAuthConfig } from "@/lib/auth/config";
import { putOAuthState } from "@/lib/auth/session-store";
import {
  cookieSecure,
  OAUTH_STATE_COOKIE,
  opaqueToken,
} from "@/lib/auth/session";
import { safeReturnTo } from "@/lib/auth/return-to";

const STATE_TTL_SECONDS = 10 * 60;

export async function GET(request: NextRequest) {
  const config = getAuthConfig();
  const state = opaqueToken();
  const codeVerifier = opaqueToken(48);
  const codeChallenge = createHash("sha256")
    .update(codeVerifier, "utf8")
    .digest("base64url");
  const expiresAt = Math.floor(Date.now() / 1000) + STATE_TTL_SECONDS;

  await putOAuthState(
    state,
    {
      codeVerifier,
      returnTo: safeReturnTo(request.nextUrl.searchParams.get("returnTo")),
    },
    expiresAt,
  );

  const authorize = new URL("/oauth2/authorize", `${config.cognitoDomain}/`);
  authorize.search = new URLSearchParams({
    client_id: config.clientId,
    code_challenge: codeChallenge,
    code_challenge_method: "S256",
    redirect_uri: authCallbackUrl(config),
    response_type: "code",
    scope: "openid email profile",
    state,
  }).toString();

  const response = NextResponse.redirect(authorize);
  response.cookies.set({
    name: OAUTH_STATE_COOKIE,
    value: state,
    httpOnly: true,
    maxAge: STATE_TTL_SECONDS,
    path: "/api/auth/callback/cognito",
    sameSite: "lax",
    secure: cookieSecure(),
  });
  return response;
}
