import { NextRequest } from "next/server";
import { getAuthConfig } from "@/lib/auth/config";
import { authenticatedSession } from "@/lib/auth/session";

const STRIPPED_REQUEST_HEADERS = [
  "authorization",
  "connection",
  "content-length",
  "cookie",
  "host",
  "keep-alive",
  "proxy-authenticate",
  "proxy-authorization",
  "te",
  "trailer",
  "transfer-encoding",
  "upgrade",
];

// 요청 쪽 `x-agora-*` 제거 규칙을 **응답에 대칭으로 옮기지 마세요.** 요청 헤더를 지우는 건
// 클라이언트가 신원·인가 입력을 스스로 정하는 걸 막기 위한 거예요. 응답 쪽 `X-Agora-*` 는
// 백엔드가 만든 값이고, 그중 `X-Agora-Excluded-Operations` 는 ZIP 다운로드에서 빠진 도구를
// 화면에 알리는 유일한 통로예요(`playground/router._EXCLUDED_OPERATIONS_HEADER`).
// 여기 넣으면 알림이 조용히 사라지고, 사용자는 줄어든 도구 집합을 모른 채 로컬을 돌려요.
const STRIPPED_RESPONSE_HEADERS = [
  "connection",
  "content-length",
  "keep-alive",
  "proxy-authenticate",
  "proxy-authorization",
  "set-cookie",
  "te",
  "trailer",
  "transfer-encoding",
  "upgrade",
];

type RouteContext = {
  params: Promise<{ path: string[] }>;
};

async function proxy(request: NextRequest, context: RouteContext): Promise<Response> {
  const session = await authenticatedSession(request);
  if (!session) {
    return Response.json(
      { detail: "로그인이 필요해요." },
      {
        status: 401,
        headers: {
          "Cache-Control": "no-store",
          "WWW-Authenticate": "Bearer",
        },
      },
    );
  }

  const { path } = await context.params;
  const { apiUrl } = getAuthConfig();
  const target = new URL(
    path.map((segment) => encodeURIComponent(segment)).join("/"),
    `${apiUrl}/`,
  );
  target.search = request.nextUrl.search;

  const headers = new Headers(request.headers);
  for (const header of STRIPPED_REQUEST_HEADERS) {
    headers.delete(header);
  }
  for (const header of [...headers.keys()]) {
    if (header.toLowerCase().startsWith("x-agora-")) {
      headers.delete(header);
    }
  }
  headers.set("Authorization", `Bearer ${session.accessToken}`);

  const upstream = await fetch(target, {
    method: request.method,
    body:
      request.method === "GET" || request.method === "HEAD"
        ? undefined
        : await request.arrayBuffer(),
    cache: "no-store",
    headers,
    redirect: "manual",
  });
  const responseHeaders = new Headers(upstream.headers);
  for (const header of STRIPPED_RESPONSE_HEADERS) {
    responseHeaders.delete(header);
  }
  responseHeaders.set("Cache-Control", "no-store");
  return new Response(upstream.body, {
    status: upstream.status,
    statusText: upstream.statusText,
    headers: responseHeaders,
  });
}

export const GET = proxy;
export const POST = proxy;
export const PUT = proxy;
export const PATCH = proxy;
export const DELETE = proxy;
export const OPTIONS = proxy;

