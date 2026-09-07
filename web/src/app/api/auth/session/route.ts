import { NextRequest, NextResponse } from "next/server";
import {
  authenticatedSession,
  clearSessionCookie,
  createSession,
  setSessionCookie,
} from "@/lib/auth/session";
import { parseSessionPayload } from "@/lib/auth/session-payload";
import { displayNameFromIdToken } from "@/lib/auth/profile";

export async function GET(request: NextRequest) {
  const session = await authenticatedSession(request);
  if (!session) {
    const response = NextResponse.json(
      { authenticated: false },
      { headers: { "Cache-Control": "no-store" } },
    );
    clearSessionCookie(response);
    return response;
  }
  return NextResponse.json(
    {
      authenticated: true,
      display_name: session.displayName,
      email: session.principal.email,
      principal_id: session.principal.principal_id,
      roles: session.principal.roles,
      team: session.principal.team,
    },
    { headers: { "Cache-Control": "no-store" } },
  );
}

export async function POST(request: NextRequest) {
  let raw: unknown;
  try {
    raw = await request.json();
  } catch {
    return NextResponse.json({ error: "invalid_json" }, { status: 400 });
  }

  const payload = parseSessionPayload(raw);
  if (!payload) {
    return NextResponse.json({ error: "invalid_tokens" }, { status: 400 });
  }

  // SRP access token엔 oauth2/userInfo용 scope가 없어 표시명 조회가 실패하므로,
  // id_token의 email/name claim에서 표시명을 뽑아 createSession에 넘긴다.
  const rawIdToken =
    raw && typeof raw === "object"
      ? (raw as Record<string, unknown>).id_token
      : undefined;
  const displayNameHint = displayNameFromIdToken(
    typeof rawIdToken === "string" ? rawIdToken : undefined,
  );

  try {
    // SRP 응답에는 token_type이 없으므로 기존 세션 계약에 맞춰 보완한다.
    // 위조 또는 다른 client의 token은 createSession의 /api/me 검증에서 거부된다.
    const { displayName, opaqueSessionId, principal } = await createSession(
      { ...payload, token_type: "Bearer" },
      { displayName: displayNameHint },
    );
    const response = NextResponse.json(
      {
        authenticated: true,
        display_name: displayName,
        email: principal.email,
        principal_id: principal.principal_id,
        roles: principal.roles,
        team: principal.team,
      },
      { headers: { "Cache-Control": "no-store" } },
    );
    setSessionCookie(response, opaqueSessionId);
    return response;
  } catch {
    const response = NextResponse.json(
      { authenticated: false, error: "rejected" },
      { status: 401 },
    );
    clearSessionCookie(response);
    return response;
  }
}
