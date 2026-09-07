import { NextRequest } from "next/server";
import { getAuthConfig } from "@/lib/auth/config";

/**
 * dev 크리덴셜 엔드포인트를 백엔드로 그대로 넘겨요.
 *
 * 이 경로들은 **사람 세션 쿠키를 쓰지 않아요** — 요청이 들고 온
 * `Authorization: Bearer <dev credential>` 을 백엔드가 직접 인증해요. 그래서 BFF 는 판단하지
 * 않고 헤더만 옮겨요. 여기서 뭔가 해석하면 인증 지점이 두 곳으로 갈라져요.
 *
 * 두 라우트(`token`·`call-handle`)가 같은 함수를 쓰는 게 중요해요. 한쪽만 `no-store` 를
 * 빼거나 `WWW-Authenticate` 를 안 넘기면, 같은 크리덴셜에 대해 두 경로가 다르게 행동해요.
 */
export async function proxyDevIdentity(
  request: NextRequest,
  path: "token" | "call-handle",
): Promise<Response> {
  const authorization = request.headers.get("authorization");
  const headers = new Headers();
  if (authorization) headers.set("Authorization", authorization);
  const contentType = request.headers.get("content-type");
  if (contentType) headers.set("Content-Type", contentType);

  try {
    const upstream = await fetch(
      `${getAuthConfig().apiUrl}/api/dev-identity/${path}`,
      {
        method: "POST",
        body: await request.arrayBuffer(),
        cache: "no-store",
        headers,
        redirect: "manual",
      },
    );
    const responseHeaders = new Headers({ "Cache-Control": "no-store" });
    const upstreamContentType = upstream.headers.get("content-type");
    if (upstreamContentType) {
      responseHeaders.set("Content-Type", upstreamContentType);
    }
    const authenticate = upstream.headers.get("www-authenticate");
    if (authenticate) responseHeaders.set("WWW-Authenticate", authenticate);
    return new Response(upstream.body, {
      status: upstream.status,
      headers: responseHeaders,
    });
  } catch {
    return Response.json(
      { detail: "dev token service unavailable" },
      { status: 502, headers: { "Cache-Control": "no-store" } },
    );
  }
}
