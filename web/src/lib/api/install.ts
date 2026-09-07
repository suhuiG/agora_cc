import { API_BASE, request } from "./client";

// 설치 (install target registry) + MCP 등록
// ---------------------------------------------------------------------------

export type OsId = "macos" | "linux" | "windows";

export type InstallTargetInfo = { tool_id: string; display_name: string };

export type InstallInstruction = {
  tool_id: string;
  asset_type: string;
  os: string;
  shell: string;
  kind: "shell" | "config";
  command: string | null;
  config_snippet: Record<string, unknown> | null;
  target_path: string;
  note: string;
};

/** 브라우저 플랫폼에서 OS를 추정해요. 실패 시 macos. */
export function detectOs(): OsId {
  const nav = navigator as Navigator & {
    userAgentData?: { platform?: string };
  };
  const p = (nav.userAgentData?.platform || nav.platform || "").toLowerCase();
  if (p.includes("win")) return "windows";
  if (p.includes("linux")) return "linux";
  return "macos";
}

/** GET /api/install/targets?asset_type= */
export function getInstallTargets(assetType: string): Promise<InstallTargetInfo[]> {
  return request<InstallTargetInfo[]>(
    `/api/install/targets?asset_type=${encodeURIComponent(assetType)}`,
  );
}

/** GET /api/assets/{id}/install — command의 {API_BASE} 플레이스홀더를 실제 origin으로 치환. */
export async function getInstallInstruction(
  recordId: string,
  opts: { tool?: string; os?: OsId; version?: string } = {},
): Promise<InstallInstruction> {
  const params = new URLSearchParams();
  params.set("tool", opts.tool ?? "claude");
  params.set("os", opts.os ?? detectOs());
  if (opts.version) params.set("version", opts.version);
  const ins = await request<InstallInstruction>(
    `/api/assets/${encodeURIComponent(recordId)}/install?${params.toString()}`,
  );
  if (ins.command) {
    ins.command = ins.command.replaceAll("{API_BASE}", API_BASE);
  }
  return ins;
}
