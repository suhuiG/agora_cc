import { NextRequest } from "next/server";
import { proxyDevIdentity } from "@/lib/api/devIdentityProxy";

export async function POST(request: NextRequest): Promise<Response> {
  return proxyDevIdentity(request, "token");
}
