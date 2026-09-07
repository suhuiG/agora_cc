// Agent Initializr — 공용 타입·모델 목록.
// 도구 목록은 getCatalog로, 파일트리는 백엔드 scaffold 응답으로 채워요(정적 mock 제거).

export type ModelOption = { id: string; label: string; note: string };

export type ToolKind = "skill" | "mcp" | "agent";
export type ToolOption = {
  // 카탈로그 record ID. scaffold 요청에서는 tool.asset_id로 전달해요.
  id: string; name: string; description: string; owner: string; kind: ToolKind;
  endpoint?: string | null;
  // skill 본문 좌표 — 백엔드가 S3에서 SKILL.md를 읽는 데 써요. 없으면 skill이 배선에서 빠져요.
  sourcePrefix?: string | null;
  version?: string | null;
};

// 제공 모델 (백엔드 MODEL_ID_MAP 키와 일치).
export const MODELS: ModelOption[] = [
  { id: "sonnet-5", label: "Claude Sonnet 5", note: "최신 · 균형" },
  { id: "opus-4-8", label: "Claude Opus 4.8", note: "최고 성능 · 복잡 추론" },
  { id: "sonnet-4-6", label: "Claude Sonnet 4.6", note: "빠름 · 범용" },
  { id: "haiku-4-5", label: "Claude Haiku 4.5", note: "초경량 · 저비용" },
];

export const TOOL_KIND_LABEL: Record<ToolKind, string> = {
  skill: "Skill",
  mcp: "MCP",
  agent: "Agent",
};

// 백엔드 scaffold 트리 노드(EXPLORE 미리보기).
export type FileNode = {
  name: string;
  path: string;
  kind: "file" | "dir";
  children?: FileNode[];
  content?: string;
};
