import { NextRequest, NextResponse } from "next/server";
import { getAuthConfig } from "@/lib/auth/config";
import {
  clearSessionCookie,
  removeRequestSession,
} from "@/lib/auth/session";

export async function GET(request: NextRequest) {
  const config = getAuthConfig();
  try {
    await removeRequestSession(request);
  } catch {
    // Browser logout remains fail-closed even if the expired record cannot be deleted.
  }

  const destination = new URL("/logout", `${config.cognitoDomain}/`);
  destination.search = new URLSearchParams({
    client_id: config.clientId,
    logout_uri: config.webBaseUrl,
  }).toString();

  const response = NextResponse.redirect(destination);
  clearSessionCookie(response);
  return response;
}
