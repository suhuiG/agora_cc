import { NextRequest } from "next/server";
import { proxyDevIdentity } from "@/lib/api/devIdentityProxy";

/**
 * 로컬에서 실행하는 생성 코드가 MCP 호출 직전에 불러요.
 *
 * `token` 과 짝이에요 — 토큰만 있고 handle 이 없으면 Gateway 가 모든 호출을 거부해요
 * (interceptor 가 호출자 신원을 handle 로만 확인해요).
 */
export async function POST(request: NextRequest): Promise<Response> {
  return proxyDevIdentity(request, "call-handle");
}
