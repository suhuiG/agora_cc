import type { AssetType } from "@/lib/api";

export type Mode =
  | "source"
  | "reference"
  | "agent-domain"
  | "agent-json"
  | "deploy"
  | "agent-deploy";

export type WizardChoice = {
  key: string;
  mode: Mode;
  assetType: AssetType;
  requiredFile: string;
  label: string;
  badge: string;
  desc: string;
};

export type FileRow = { id: string; path: string; content: string };

export type PublishForm = {
  name: string;
  description: string;
  version: string;
  category: string;
  owner_team: string;
  escalation_contact: string;
  tags: string;
  changelog: string;
};

export const CHOICES: WizardChoice[] = [
  {
    key: "skill",
    mode: "source",
    assetType: "skill",
    requiredFile: "SKILL.md",
    label: "Skill",
    badge: "소스 업로드",
    desc: "마크다운 기반 에이전트 스킬이에요. SKILL.md 를 포함한 소스 파일을 업로드해요.",
  },
  {
    key: "mcp-connect",
    mode: "reference",
    assetType: "mcp",
    requiredFile: "",
    label: "MCP (연결형)",
    badge: "Gateway 연결",
    desc: "이미 호스팅 중인 MCP 서버의 endpoint 를 등록해요. connect 테스트 후 Gateway에 연결하고, 카탈로그에 등재돼요.",
  },
  {
    key: "mcp-deploy",
    mode: "deploy",
    assetType: "mcp",
    requiredFile: "",
    label: "MCP (배포형)",
    badge: "Lambda · Gateway",
    desc: "로컬 MCP 소스 폴더를 올리면 승인 후 Lambda tool-provider로 빌드해 Gateway에 연결하고 endpoint를 발급해요.",
  },
  {
    key: "agent-domain",
    mode: "agent-domain",
    assetType: "agent",
    requiredFile: "",
    label: "Agent (도메인 연결)",
    badge: "A2A · 도메인",
    desc: "이미 떠 있는 A2A 에이전트의 도메인을 연결해요. connect 테스트로 agent-card를 확인한 뒤 등재돼요.",
  },
  {
    key: "agent-deploy",
    mode: "agent-deploy",
    assetType: "agent",
    requiredFile: "agent-card.json",
    label: "Agent (배포형)",
    badge: "AgentCore Runtime",
    desc: "A2A 에이전트 소스를 업로드하면 승인 후 AgentCore Runtime에 호스팅하고 invoke URL을 발급해요.",
  },
];

export function newRow(path = "", content = ""): FileRow {
  return {
    id:
      typeof crypto !== "undefined" && "randomUUID" in crypto
        ? crypto.randomUUID()
        : `row-${Math.random().toString(36).slice(2)}`,
    path,
    content,
  };
}

export function parseFrontmatter(md: string): {
  name?: string;
  description?: string;
  version?: string;
  changelog?: string;
} {
  const match = md.match(/^---\r?\n([\s\S]*?)\r?\n---/);
  if (!match) return {};
  const values: Record<string, string> = {};
  for (const line of match[1].split(/\r?\n/)) {
    const keyValue = line.match(/^([A-Za-z][\w-]*):\s*(.*)$/);
    if (keyValue) {
      values[keyValue[1].toLowerCase()] = keyValue[2]
        .trim()
        .replace(/^["']|["']$/g, "");
    }
  }
  return {
    name: values.name,
    description: values.description,
    version: values.version,
    changelog: values.changelog ?? values.changes,
  };
}

export function unsafePathReason(path: string): string | null {
  if (!path.trim()) return "파일 이름이 비어 있어요.";
  if (path.startsWith("/")) return "파일 경로는 '/' 로 시작할 수 없어요.";
  if (path.includes("..")) return "파일 경로에 '..' 는 쓸 수 없어요.";
  if (path.includes("\\")) return "파일 경로에 역슬래시(\\)는 쓸 수 없어요.";
  return null;
}
