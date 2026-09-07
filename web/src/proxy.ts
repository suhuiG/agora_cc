import { NextRequest, NextResponse } from "next/server";

const SESSION_COOKIE = "agora_session";

export function proxy(request: NextRequest) {
  if (request.cookies.has(SESSION_COOKIE)) {
    return NextResponse.next();
  }

  const login = new URL("/login", request.url);
  login.searchParams.set(
    "returnTo",
    `${request.nextUrl.pathname}${request.nextUrl.search}`,
  );
  return NextResponse.redirect(login);
}

export const config = {
  matcher: [
    "/catalog/:path*",
    "/evaluation/:path*",
    "/governance/:path*",
    "/playground/:path*",
    "/runtime/:path*",
  ],
};
