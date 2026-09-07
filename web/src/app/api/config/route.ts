import { NextResponse } from "next/server";
import { getAuthConfig } from "@/lib/auth/config";
import { toPublicConfig } from "@/lib/auth/public-config";

export async function GET() {
  const { userPoolId, clientId, region } = getAuthConfig();

  return NextResponse.json(toPublicConfig({ userPoolId, clientId, region }), {
    headers: { "Cache-Control": "no-store" },
  });
}
