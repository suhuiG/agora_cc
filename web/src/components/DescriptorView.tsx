"use client";

// 자산 타입별 descriptor 를 구조화해 보여주는 컴포넌트예요.
// 기존 상세 페이지는 raw JSON 이나 "S3에 있어요" 안내만 보여줬는데,
// 비-IT 사용자가 "이 도구가 뭘 하는지"를 바로 이해하도록 tool 목록·능력·메타를 구조화해요.
//
// descriptor 모양 (백엔드 _build_descriptors / seed 기준):
//  MCP   : { mcp:   { server:{inlineContent:json}, tools:{inlineContent:json}, sourcePrefix? } }
//  Agent : { agent: { agentCard:{capabilities[],endpoint,...}, sourcePrefix? } }
//  Skill : { skill: { markdown, sourcePrefix? } }
//  App   : { app:   { endpoint, runtime, hosting } }
//  Model : { model: { provider, modelId, hostedBy, modalities[] } }

import Link from "next/link";
import { Card } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";

interface DescriptorViewProps {
  descriptorType: string;
  descriptors: Record<string, unknown>;
  /** 소스 모드면 파일 트리가 S3 에 있어요(보조 안내용). */
  sourcePrefix?: string | null;
  /** 소스 모드 skill의 SKILL.md 본문(S3에서 읽어온 것). descriptors.skill.markdown 대체. */
  skillMarkdown?: string | null;
}

// ── MCP tool 파싱 ──────────────────────────────────────────────────
interface McpTool {
  name: string;
  description?: string;
  params: { name: string; type?: string; required: boolean; enum?: string[] }[];
}

// mcp.tools.inlineContent 는 `{"tools":[{name,description,inputSchema}]}` 형태의 JSON 문자열이에요.
function parseMcpTools(descriptors: Record<string, unknown>): McpTool[] | null {
  const mcp = descriptors.mcp as Record<string, unknown> | undefined;
  const tools = mcp?.tools as Record<string, unknown> | undefined;
  const inline = tools?.inlineContent;
  if (typeof inline !== "string") return null;
  try {
    const parsed = JSON.parse(inline) as {
      tools?: {
        name: string;
        description?: string;
        inputSchema?: { properties?: Record<string, { type?: string; enum?: string[] }>; required?: string[] };
      }[];
    };
    if (!Array.isArray(parsed.tools)) return null;
    return parsed.tools.map((t) => {
      const props = t.inputSchema?.properties ?? {};
      const required = new Set(t.inputSchema?.required ?? []);
      const params = Object.entries(props).map(([pname, p]) => ({
        name: pname,
        type: p.type,
        required: required.has(pname),
        enum: p.enum,
      }));
      // 필수 파라미터가 위로 오도록 정렬(필수 우선, 그 안에서는 원래 순서 유지).
      params.sort((a, b) => Number(b.required) - Number(a.required));
      return {
        name: t.name,
        description: t.description,
        params,
      };
    });
  } catch {
    return null;
  }
}

function McpToolsView({ descriptors, sourcePrefix }: { descriptors: Record<string, unknown>; sourcePrefix?: string | null }) {
  const tools = parseMcpTools(descriptors);

  // 연결 endpoint는 상세 페이지의 "설치" 카드에서 복사할 수 있게 노출하므로,
  // 이 tool 정의 박스 안에서는 endpoint를 표시하지 않아요(중복 제거).

  if (!tools || tools.length === 0) {
    return (
      <div className="space-y-3">
        <p className="text-sm text-muted-foreground">
          tool 정의가 아직 없어요.
          {sourcePrefix && " 소스는 S3에 저장돼 있어요."}
        </p>
      </div>
    );
  }
  return (
    <div className="space-y-3">
      {tools.map((tool) => (
        <Card key={tool.name} className="p-4">
          <div className="mb-1 flex items-center gap-2">
            <code className="rounded bg-muted px-1.5 py-0.5 font-mono text-sm font-semibold text-foreground">
              {tool.name}
            </code>
          </div>
          {tool.description && (
            <p className="mb-3 text-sm text-muted-foreground line-clamp-2" title={tool.description}>
              {tool.description}
            </p>
          )}
          {tool.params.length > 0 && (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-border text-left text-xs text-muted-foreground">
                    <th className="py-1.5 pr-4 font-medium">파라미터</th>
                    <th className="py-1.5 pr-4 font-medium">타입</th>
                    <th className="py-1.5 pr-4 font-medium">필수</th>
                    <th className="py-1.5 font-medium">허용값</th>
                  </tr>
                </thead>
                <tbody>
                  {tool.params.map((p) => (
                    <tr key={p.name} className="border-b border-border/60 last:border-0">
                      <td className="py-1.5 pr-4 font-mono text-xs text-foreground">{p.name}</td>
                      <td className="py-1.5 pr-4 text-muted-foreground">{p.type ?? "—"}</td>
                      <td className="py-1.5 pr-4">
                        {p.required ? (
                          <span className="text-xs font-medium text-red-600">필수</span>
                        ) : (
                          <span className="text-xs text-muted-foreground">선택</span>
                        )}
                      </td>
                      <td className="py-1.5">
                        {p.enum ? (
                          <span className="flex flex-wrap gap-1">
                            {p.enum.map((e) => (
                              <Badge key={e} variant="tag">{e}</Badge>
                            ))}
                          </span>
                        ) : (
                          <span className="text-muted-foreground">—</span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>
      ))}
    </div>
  );
}

// ── Agent (A2A) 뷰 ─────────────────────────────────────────────────
interface AgentSkill {
  id: string;
  name: string;
  description: string;
  tags: string[];
}

/** agent 가 의존하는 MCP 자산. `descriptors.agent.agoraDependencies.mcpAssets[]` 가 정본이에요. */
type AgentMcpAsset = {
  /** 카탈로그 record_id. 상세로 링크할 좌표예요. */
  assetId: string;
  name: string;
  version: string;
  /** 이 agent 가 승인받은 operation 만. 자산 전체 도구 목록이 아니에요. */
  operations: string[];
  /** 민감도 슬라이스 Target (IA-52). 어느 슬라이스로 인가됐는지가 여기 드러나요. */
  targets: { name: string; sensitivity: string }[];
};

function parseMcpAssets(node: unknown): AgentMcpAsset[] {
  const deps = node as Record<string, unknown> | undefined;
  const raw = Array.isArray(deps?.mcpAssets) ? (deps.mcpAssets as unknown[]) : [];
  return raw
    .filter((a): a is Record<string, unknown> => typeof a === "object" && a !== null)
    .map((a) => {
      const targetsRaw = Array.isArray(a.gatewayTargets)
        ? (a.gatewayTargets as unknown[])
        : [];
      return {
        assetId: String(a.assetId ?? ""),
        name: String(a.name ?? a.assetId ?? ""),
        version: String(a.version ?? ""),
        operations: Array.isArray(a.operations)
          ? a.operations.map((o) => String(o))
          : [],
        targets: targetsRaw
          .filter((t): t is Record<string, unknown> =>
            typeof t === "object" && t !== null)
          .map((t) => ({
            name: String(t.name ?? ""),
            sensitivity: String(t.sensitivity ?? ""),
          }))
          .filter((t) => t.name),
      };
    })
    .filter((a) => a.name);
}

// A2A capabilities는 { streaming, pushNotifications, ... } 같은 boolean 맵이에요.
// true인 키만 뽑아 배지로 보여줘요(배열 형태로 온 옛 데이터도 지원).
function capabilityLabels(cap: unknown): string[] {
  if (Array.isArray(cap)) return cap.map((c) => String(c));
  if (cap && typeof cap === "object") {
    return Object.entries(cap as Record<string, unknown>)
      .filter(([, v]) => v === true)
      .map(([k]) => k);
  }
  return [];
}

// descriptors.agent.agentCard 는 { inlineContent: JSON 문자열 } (등록 경로) 형태이거나,
// seed 데이터처럼 capabilities/skills를 직접 담은 객체일 수 있어요. 둘 다 지원해요.
function parseAgentCard(descriptors: Record<string, unknown>): {
  capabilities: string[];
  skills: AgentSkill[];
  mcpAssets: AgentMcpAsset[];
  endpoint?: string;
  hasCard: boolean;
} {
  const agent = descriptors.agent as Record<string, unknown> | undefined;
  const cardNode = agent?.agentCard as Record<string, unknown> | undefined;
  const endpoint = (agent?.endpoint ?? cardNode?.endpoint) as string | undefined;
  // MCP 의존은 카드가 아니라 `descriptors.agent.agoraDependencies` 가 정본이에요.
  // 카드가 없어도 이건 읽을 수 있어야 해요.
  const mcpAssets = parseMcpAssets(agent?.agoraDependencies);
  if (!cardNode) {
    return { capabilities: [], skills: [], mcpAssets, endpoint, hasCard: false };
  }

  // inlineContent(JSON 문자열)가 있으면 원본 카드를 파싱, 없으면 노드 자체를 카드로.
  let card: Record<string, unknown> = cardNode;
  const inline = cardNode.inlineContent;
  if (typeof inline === "string") {
    try {
      card = JSON.parse(inline) as Record<string, unknown>;
    } catch {
      /* 파싱 실패 → cardNode의 표층 필드로 폴백 */
    }
  }

  const skillsRaw = Array.isArray(card.skills) ? (card.skills as unknown[]) : [];
  const skills: AgentSkill[] = skillsRaw
    .filter((s): s is Record<string, unknown> => typeof s === "object" && s !== null)
    .map((s) => ({
      id: String(s.id ?? s.name ?? ""),
      name: String(s.name ?? s.id ?? ""),
      description: String(s.description ?? ""),
      tags: Array.isArray(s.tags) ? s.tags.map((t) => String(t)) : [],
    }));

  return {
    capabilities: capabilityLabels(card.capabilities),
    skills,
    mcpAssets,
    endpoint,
    hasCard: true,
  };
}

function AgentView({ descriptors, sourcePrefix }: { descriptors: Record<string, unknown>; sourcePrefix?: string | null }) {
  // 호출 endpoint는 상세 페이지의 "호출 endpoint" 복사 카드에서 노출하므로,
  // 이 에이전트 스펙 박스 안에서는 표시하지 않아요(중복 제거).
  const { capabilities, skills, mcpAssets, hasCard } = parseAgentCard(descriptors);
  // scaffold 는 MCP 의존을 A2A `skills[]` 에 `tags: ["mcp"]` 로도 적어요. 그대로 두면 같은
  // 자산이 "스킬" 로 보이고 링크도 없어서, 사용자가 MCP 상세로 갈 방법이 없어요. 정본인
  // `agoraDependencies.mcpAssets` 로 별도 섹션을 만들고 여기서는 그 중복을 걸러요.
  const mcpNames = new Set(mcpAssets.map((a) => a.name));
  const pureSkills = skills.filter(
    (s) => !(s.tags.includes("mcp") || mcpNames.has(s.name)),
  );

  if (!hasCard && mcpAssets.length === 0) {
    return (
      <p className="text-sm text-muted-foreground">
        agent-card 정의가 아직 없어요.{sourcePrefix && " 소스는 S3에 저장돼 있어요."}
      </p>
    );
  }
  return (
    <div className="space-y-4">
      {/* skills — A2A 카드의 자연어 능력(id/name/description/tags) */}
      {/* MCP 의존 — agoraDependencies.mcpAssets 가 정본. 카탈로그 상세로 링크해요. */}
      {mcpAssets.length > 0 && (
        <div>
          <div className="mb-2 text-sm font-medium text-foreground">MCP</div>
          <div className="space-y-3">
            {mcpAssets.map((mcp) => (
              <Card key={mcp.assetId || mcp.name} className="p-4">
                <div className="mb-1 flex flex-wrap items-center gap-2">
                  {mcp.assetId ? (
                    <Link
                      href={`/catalog/assets/${mcp.assetId}`}
                      className="rounded bg-muted px-1.5 py-0.5 font-mono text-sm font-semibold text-blue-700 hover:underline"
                    >
                      {mcp.name}
                    </Link>
                  ) : (
                    <code className="rounded bg-muted px-1.5 py-0.5 font-mono text-sm font-semibold text-foreground">
                      {mcp.name}
                    </code>
                  )}
                  {mcp.version && <Badge variant="tag">v{mcp.version}</Badge>}
                </div>
                {mcp.operations.length > 0 && (
                  <div className="mb-2">
                    <div className="mb-1 text-xs text-muted-foreground">
                      승인된 operation
                    </div>
                    <div className="flex flex-wrap gap-1">
                      {mcp.operations.map((op) => (
                        <code
                          key={op}
                          className="rounded bg-muted px-1.5 py-0.5 font-mono text-xs text-foreground"
                        >
                          {op}
                        </code>
                      ))}
                    </div>
                  </div>
                )}
                {mcp.targets.length > 0 && (
                  <div>
                    <div className="mb-1 text-xs text-muted-foreground">
                      민감도 Target
                    </div>
                    <div className="flex flex-wrap gap-1">
                      {mcp.targets.map((t) => (
                        <Badge key={t.name} variant="tag">
                          {t.name}
                          {t.sensitivity ? ` · ${t.sensitivity}` : ""}
                        </Badge>
                      ))}
                    </div>
                  </div>
                )}
              </Card>
            ))}
          </div>
        </div>
      )}

      <div>
        <div className="mb-2 text-sm font-medium text-foreground">스킬 (skills)</div>
        {pureSkills.length > 0 ? (
          <div className="space-y-3">
            {pureSkills.map((skill) => (
              <Card key={skill.id} className="p-4">
                <div className="mb-1 flex items-center gap-2">
                  <code className="rounded bg-muted px-1.5 py-0.5 font-mono text-sm font-semibold text-foreground">
                    {skill.name}
                  </code>
                </div>
                {skill.description && (
                  <p className="mb-2 text-sm text-muted-foreground" title={skill.description}>
                    {skill.description}
                  </p>
                )}
                {skill.tags.length > 0 && (
                  <div className="flex flex-wrap gap-1">
                    {skill.tags.map((tag) => (
                      <Badge key={tag} variant="tag">{tag}</Badge>
                    ))}
                  </div>
                )}
              </Card>
            ))}
          </div>
        ) : (
          <p className="text-sm text-muted-foreground">표시할 스킬이 없어요.</p>
        )}
      </div>

      {/* capabilities — streaming/pushNotifications 등 프로토콜 능력 플래그 */}
      {capabilities.length > 0 && (
        <div>
          <div className="mb-2 text-sm font-medium text-foreground">프로토콜 능력</div>
          <div className="flex flex-wrap gap-1.5">
            {capabilities.map((c) => (
              <Badge key={c} variant="tag">{c}</Badge>
            ))}
          </div>
        </div>
      )}

    </div>
  );
}

// ── App 뷰 ─────────────────────────────────────────────────────────
function AppView({ descriptors }: { descriptors: Record<string, unknown> }) {
  const app = descriptors.app as Record<string, unknown> | undefined;
  const endpoint = app?.endpoint as string | undefined;
  const runtime = app?.runtime as string | undefined;
  const hosting = app?.hosting as string | undefined;
  return (
    <div className="space-y-3">
      <p className="text-sm text-muted-foreground">
        LLM 호출이 없는 일반 앱이에요. 별도 호스팅으로 배포돼 endpoint로 제공돼요.
      </p>
      <dl className="grid grid-cols-2 gap-3 text-sm md:grid-cols-3">
        <Meta label="런타임" value={runtime ?? "—"} />
        <Meta label="호스팅" value={hosting === "TBD" ? "결정 예정 (Phase 2)" : hosting ?? "—"} />
      </dl>
      {endpoint && <EndpointRow label="endpoint" value={endpoint} />}
    </div>
  );
}

// ── Model 뷰 ───────────────────────────────────────────────────────
function ModelView({ descriptors }: { descriptors: Record<string, unknown> }) {
  const model = descriptors.model as Record<string, unknown> | undefined;
  const provider = model?.provider as string | undefined;
  const modelId = model?.modelId as string | undefined;
  const hostedBy = model?.hostedBy as string | undefined;
  const modalities = (model?.modalities as string[] | undefined) ?? [];
  return (
    <div className="space-y-3">
      <p className="text-sm text-muted-foreground">
        외부(Bedrock 등)가 호스팅하는 모델 참조예요. 플레이그라운드에서 선택해 써요.
      </p>
      <dl className="grid grid-cols-2 gap-3 text-sm md:grid-cols-3">
        <Meta label="제공자" value={provider ?? "—"} />
        <Meta label="호스팅" value={hostedBy ?? "—"} />
        <Meta label="모달리티" value={modalities.join(", ") || "—"} />
      </dl>
      {modelId && <EndpointRow label="모델 ID" value={modelId} />}
    </div>
  );
}

// ── Skill (마크다운) 뷰 ────────────────────────────────────────────
function SkillView({
  descriptors,
  sourcePrefix,
  skillMarkdown,
}: {
  descriptors: Record<string, unknown>;
  sourcePrefix?: string | null;
  skillMarkdown?: string | null;
}) {
  const skill = descriptors.skill as Record<string, unknown> | undefined;
  const inlineMd = skill?.markdown;
  // S3에서 읽어온 본문(skillMarkdown)을 우선, 없으면 인라인 descriptors.skill.markdown.
  const markdown =
    typeof skillMarkdown === "string" && skillMarkdown.length > 0
      ? skillMarkdown
      : typeof inlineMd === "string" && inlineMd.length > 0
        ? inlineMd
        : null;
  if (markdown) {
    return (
      <pre className="max-h-[480px] overflow-auto whitespace-pre-wrap rounded-lg bg-muted p-4 text-sm">
        {markdown}
      </pre>
    );
  }
  // 소스 모드인데 아직 본문을 못 읽어온 경우(로딩 중이거나 실패).
  return (
    <p className="text-sm text-muted-foreground">
      {sourcePrefix ? "SKILL.md 본문을 불러오는 중이거나 읽을 수 없어요." : "SKILL.md 본문이 인라인에 없어요."}
    </p>
  );
}

// ── 공통 작은 조각 ─────────────────────────────────────────────────
function Meta({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="font-medium text-foreground">{value}</dd>
    </div>
  );
}

function EndpointRow({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div className="mb-1 text-xs text-muted-foreground">{label}</div>
      <code className="block overflow-x-auto rounded-lg bg-muted px-3 py-2 font-mono text-xs text-foreground">
        {value}
      </code>
    </div>
  );
}

export function DescriptorView({ descriptorType, descriptors, sourcePrefix, skillMarkdown }: DescriptorViewProps) {
  switch (descriptorType) {
    case "MCP":
      return <McpToolsView descriptors={descriptors} sourcePrefix={sourcePrefix} />;
    case "Agent":
      return <AgentView descriptors={descriptors} sourcePrefix={sourcePrefix} />;
    case "Agent Skills":
      return <SkillView descriptors={descriptors} sourcePrefix={sourcePrefix} skillMarkdown={skillMarkdown} />;
    case "App":
      return <AppView descriptors={descriptors} />;
    case "Model":
      return <ModelView descriptors={descriptors} />;
    default:
      // 알 수 없는 타입은 raw JSON 으로 폴백해요.
      return (
        <pre className="overflow-auto whitespace-pre-wrap rounded-lg bg-muted p-4 text-sm">
          {JSON.stringify(descriptors, null, 2)}
        </pre>
      );
  }
}
