"use client";

import { useRouter } from "next/navigation";
import { useEffect, useMemo, useRef, useState } from "react";
import { ExploreModal } from "./ExploreModal";
import {
  ChipGroup,
  Field,
  RadioCard,
  RangeRow,
  Section,
  SettingSection,
} from "./controls";
import { SensitivityBadge } from "@/components/SensitivityBadge";
import { Button } from "@/components/ui/button";
import { Icon } from "@/components/ui/icon";
import { Input } from "@/components/ui/input";
import { Modal } from "@/components/ui/modal";
import { useToast } from "@/components/ui/toast";
import {
  ApiError,
  checkAgentName,
  downloadScaffold,
  generatePrompt,
  getCatalog,
  getScaffold,
  nameConflictMessage,
  SESSION_PRINCIPAL,
  startAgentDeploy,
  uploadAgentFolder,
  type AssetCard,
  type MemoryStrategy,
  type ScaffoldFileNode,
} from "@/lib/api";
import { scaffoldDownloadFailureMessage } from "@/lib/api/scaffold-download";
import { MODELS, type ToolKind, type ToolOption } from "@/lib/initializr";
import { cn } from "@/lib/ui";
import { AddCatalogToolsModal } from "./AddCatalogToolsModal";
import {
  attemptScaffoldDownload,
  type ScaffoldDownloadAttempt,
} from "./download";
import {
  PromptRequestGate,
  isNameDeployable,
  nameAvailabilityAfterCheck,
  type NameAvailability,
} from "./lifecycle";
import {
  MCP_EMPTY_SELECTION_MESSAGE,
  MEMORY_RETENTION_MAX,
  MEMORY_RETENTION_MIN,
  MEMORY_STRATEGIES,
  SLIDING_WINDOW_MIN,
  buildAgentToolRequests,
  buildScaffoldSpec,
  missingJustifications,
  operationDeploymentApprovalLabel,
  operationLocalDownloadLabel,
  shouldIncludeDevIdentity,
  validateMemorySettings,
  visibleOperations,
  type ContextStrategy,
  type MemoryMode,
  type OperationJustifications,
  type SelectedCatalogTool,
} from "./model";
// 등록 폼과 같은 회원 검색 피커를 재사용해요. 자유 입력을 허용하면 승인 계약이
// 조용히 막혀요(CA-28·CA-29 ②). 세 번째 소비자가 생기면 공용 위치로 옮겨요.
import { EscalationContactPicker }
  from "@/app/(portal)/catalog/publish/_components/EscalationContactPicker";

const TEMPERATURE_NOTE: Record<string, string> = {
  "sonnet-5": "temperature 미지원",
  "opus-4-8": "temperature 미지원",
  "sonnet-4-6": "temperature 0.2 고정",
  "haiku-4-5": "temperature 0.2 고정",
};
/*
 * IH-101 temporary disablement (2026-08-22): strands-agents-tools built-in
 * extras require bedrock-agentcore<1.2.0. Re-enable this retained UI only when
 * those extras permit the qualified bedrock-agentcore>=1.22.0.
type BuiltinTool = "browser" | "code_interpreter";
 */
type InfoKey = "memory" | "context" | "limits";

const INFO_CONTENT: Record<
  InfoKey,
  { title: string; caption: string; items: { term: string; desc: string }[] }
> = {
  memory: {
    title: "기억 (Memory)",
    caption: "세션을 넘어 어떤 대화와 정보를 남길지 정해요.",
    items: [
      {
        term: "사용 안 함 (DISABLED)",
        desc: "Memory 리소스를 만들지 않아 과금이 없어요. 세션이 끝나면 대화가 남지 않는 기본값이에요.",
      },
      {
        term: "AWS 관리형 (MANAGED)",
        desc: "agent 전용 AgentCore Memory 리소스를 만드는 과금 대상 옵션이에요. 세션 안의 turn 이벤트를 단기 기억으로 쓰고, 장기 전략을 켜면 세션을 넘어 장기 기억도 유지해요.",
      },
      {
        term: "장기 전략",
        desc: "공식 AgentCore Memory 계약상 배열이므로 여러 전략을 함께 사용할 수 있어요.",
      },
      {
        term: "SEMANTIC",
        desc: "대화에서 사실과 지식을 추출해 벡터로 저장하고 검색형 회상에 사용해요.",
      },
      {
        term: "SUMMARIZATION",
        desc: "대화 요약을 만들어 긴 대화의 맥락을 유지해요.",
      },
      {
        term: "보존 기간",
        desc: "Memory 이벤트가 만료되는 기간이며 3일에서 365일까지 설정할 수 있어요.",
      },
    ],
  },
  context: {
    title: "context 관리",
    caption: "기억이 세션을 넘어 무엇을 남길지 정한다면, context 관리는 이번 요청에서 모델에 무엇을 넣을지 정해요.",
    items: [
      {
        term: "Sliding window",
        desc: "최근 window_size개의 메시지만 모델에 넘기고 오래된 메시지는 버려요. 예측 가능하고 비용이 낮지만 오래된 맥락은 사라져요.",
      },
      {
        term: "관리 안 함",
        desc: "대화를 자르지 않아요. 대화가 길어지면 모델의 context 한도에 부딪힐 수 있어요.",
      },
    ],
  },
  limits: {
    title: "실행 상한",
    caption: "응답과 agent 반복 실행의 상한을 정해 폭주와 과도한 비용을 막아요.",
    items: [
      {
        term: "max tokens",
        desc: "한 응답이 생성할 수 있는 최대 토큰 수예요. 너무 낮으면 답변이 중간에 잘릴 수 있어요.",
      },
      {
        term: "max iterations",
        desc: "agent 루프의 최대 반복, 즉 도구 호출 왕복 횟수의 상한이에요. 여러 도구를 순서대로 써야 하는 작업은 필요한 반복 수보다 낮으면 미완성으로 끝나요.",
      },
    ],
  },
};

/*
 * IH-101 re-enable with BuiltinTool after the extras accept
 * bedrock-agentcore>=1.22.0.
const BUILTIN_TOOLS: {
  id: BuiltinTool;
  name: string;
  product: string;
  description: string;
}[] = [
  {
    id: "browser",
    name: "브라우저",
    product: "AgentCore Browser · 세션 기반",
    description: "실제 브라우저 세션을 열어 로그인이 필요한 페이지나 동적 사이트를 다뤄요.",
  },
  {
    id: "code_interpreter",
    name: "코드 인터프리터",
    product: "AgentCore Code Interpreter",
    description: "파이썬을 실행해 계산·데이터 분석·검산을 해요.",
  },
];
 */

function kindOf(descriptorType: string): ToolKind {
  return descriptorType === "MCP" ? "mcp" : "skill";
}

function cardToTool(card: AssetCard): ToolOption {
  return {
    id: card.record_id,
    name: card.name,
    description: card.description,
    owner: card.owner_team || card.owner_user || "unknown",
    kind: kindOf(card.descriptor_type),
    endpoint: card.endpoint ?? null,
    sourcePrefix: card.source_prefix || null,
    version: card.version || null,
  };
}

function flattenTree(nodes: ScaffoldFileNode[]): { path: string; content: string }[] {
  return nodes.flatMap((node) => [
    ...(node.kind === "file"
      ? [{ path: node.path, content: node.content ?? "" }]
      : []),
    ...(node.children ? flattenTree(node.children) : []),
  ]);
}

export function InitializrStrandsClient() {
  const router = useRouter();
  const mountedRef = useRef(true);
  const promptGateRef = useRef<PromptRequestGate | null>(null);
  const nameCheckIdRef = useRef(0);
  if (promptGateRef.current === null) {
    promptGateRef.current = new PromptRequestGate();
  }
  const [model, setModel] = useState(MODELS[0].id);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  // 2차 담당자(에스컬레이션). 백엔드 `/api/agent/deploy/init` 이 필수로 요구하는데
  // (`runtime/router.py:362` `_require_responsibility_contacts`) 이 화면이 수집하지 않아서
  // 배포가 항상 422 로 막혀 있었어요(2026-08-29 실측). 등록 폼과 같은 회원 검색 피커를
  // 재사용해요 — 자유 입력을 허용하면 승인 계약이 조용히 막혀요(CA-28·CA-29 ②).
  const [escalationContact, setEscalationContact] = useState("");
  const [nameAvailability, setNameAvailability] =
    useState<NameAvailability>("unchecked");
  const [nameConflict, setNameConflict] = useState("");
  const [memoryMode, setMemoryMode] = useState<MemoryMode>("DISABLED");
  const [memoryStrategies, setMemoryStrategies] = useState(
    new Set<MemoryStrategy>(["SEMANTIC", "SUMMARIZATION"]),
  );
  const [memoryRetentionDays, setMemoryRetentionDays] = useState(30);
  const [contextStrategy, setContextStrategy] =
    useState<ContextStrategy>("sliding_window");
  const [windowSize, setWindowSize] = useState(40);
  const [maxTokensK, setMaxTokensK] = useState(16);
  const [maxIterations, setMaxIterations] = useState(12);
  const [catalogTools, setCatalogTools] = useState<ToolOption[]>([]);
  const [toolsLoading, setToolsLoading] = useState(true);
  const [selectedTools, setSelectedTools] = useState<SelectedCatalogTool[]>([]);
  /*
   * IH-101 re-enable with the commented built-in tool UI after compatible
   * strands-agents-tools extras are published.
  const [builtinTools, setBuiltinTools] = useState(new Set<BuiltinTool>());
   */
  const [infoKey, setInfoKey] = useState<InfoKey | null>(null);
  // IH-101 re-enable with the commented built-in tool information modal.
  // const [showBuiltinInfo, setShowBuiltinInfo] = useState(false);
  const [justifications, setJustifications] =
    useState<OperationJustifications>({});
  const [showTools, setShowTools] = useState(false);
  const [systemPrompt, setSystemPrompt] = useState("");
  const [promptGenerated, setPromptGenerated] = useState(false);
  const [promptGenerating, setPromptGenerating] = useState(false);
  const [showExplore, setShowExplore] = useState(false);
  const [exploreFiles, setExploreFiles] = useState<ScaffoldFileNode[]>([]);
  const [exploreError, setExploreError] = useState("");
  const [credentialFallback, setCredentialFallback] = useState<
    Extract<ScaffoldDownloadAttempt, { kind: "credential_unavailable" }> | null
  >(null);
  const [codeOnlyDownloading, setCodeOnlyDownloading] = useState(false);
  const [deploying, setDeploying] = useState(false);
  const [deployError, setDeployError] = useState("");
  const { success, error: toastError, toast } = useToast();

  useEffect(() => {
    let active = true;
    void Promise.all([
      getCatalog("Agent Skills", 0, 50),
      getCatalog("MCP", 0, 50),
    ]).then(([skills, mcps]) => {
      if (active) {
        setCatalogTools([...skills.items, ...mcps.items].map(cardToTool));
      }
    }).catch(() => {
      if (active) setCatalogTools([]);
    }).finally(() => {
      if (active) setToolsLoading(false);
    });
    return () => { active = false; };
  }, []);

  useEffect(() => {
    // React Strict Mode(app router 기본값)는 dev에서 effect를 mount→cleanup→mount로
    // 두 번 돌려요. cleanup만 `false`로 두면 remount 후에도 계속 false라
    // mountedRef를 보는 모든 상태 반영이 조용히 스킵돼요(이름 검사·프롬프트 생성·
    // 배포가 영구히 진행 중으로 멈춤). 그래서 effect 본문에서 다시 true로 세워요.
    mountedRef.current = true;
    // 같은 이유로 gate도 되살려요. cleanup의 dispose()는 `disposed`를 영구히 세우는데
    // gate는 ref가 null일 때만 만들어지므로 remount 후에도 닫힌 인스턴스가 남아요.
    // 그러면 canApply()가 false라 생성된 프롬프트가 버려지고 스피너가 안 풀려요.
    promptGateRef.current?.reset();
    return () => {
      mountedRef.current = false;
      promptGateRef.current?.dispose();
    };
  }, []);

  useEffect(() => {
    promptGateRef.current?.invalidate();
  }, [name, description, selectedTools, systemPrompt]);

  const spec = useMemo(() => buildScaffoldSpec({
    name,
    model,
    description,
    systemPrompt,
    tools: selectedTools,
    contextStrategy,
    windowSize,
    summaryRatio: 0.3,
    preserveRecentMessages: 10,
    maxTokens: maxTokensK * 1000,
    maxIterations,
    memoryMode,
    memoryStrategies,
    memoryRetentionDays,
    // IH-101 re-enable after compatible extras: builtinTools,
  }), [
    name,
    model,
    description,
    systemPrompt,
    selectedTools,
    contextStrategy,
    windowSize,
    maxTokensK,
    maxIterations,
    memoryMode,
    memoryStrategies,
    memoryRetentionDays,
  ]);

  const memoryErrors = useMemo(() => validateMemorySettings({
    mode: memoryMode,
    strategies: memoryStrategies,
    retentionDays: memoryRetentionDays,
  }), [
    memoryMode,
    memoryStrategies,
    memoryRetentionDays,
  ]);
  const missingReasons = useMemo(
    () => missingJustifications(selectedTools, justifications),
    [selectedTools, justifications],
  );
  const operationCount = selectedTools.reduce(
    (total, tool) => total + (
      tool.kind === "mcp" ? tool.selectedOperations.size : 0
    ),
    0,
  );
  const skillCount = selectedTools.filter((tool) => tool.kind === "skill").length;

  /*
   * IH-101 re-enable with BuiltinTool state after compatible extras.
  function toggleBuiltinTool(tool: BuiltinTool) {
    setBuiltinTools((current) => {
      const next = new Set(current);
      if (next.has(tool)) next.delete(tool);
      else next.add(tool);
      return next;
    });
  }
   */
  const canDeploy = Boolean(
    name.trim()
    && description.trim()
    && systemPrompt.trim()
    && escalationContact.trim()
    && isNameDeployable(nameAvailability)
    && missingReasons.length === 0
    && memoryErrors.length === 0
    && !deploying
  );
  // IH-50/IH-76: 버튼이 왜 비활성인지 화면에서 알려줘요. 조용히 비활성이면 사용자는
  // 무엇을 고쳐야 할지 알 수 없어요(특히 정체된 배포 checkpoint는 원인이 안 보여요).
  const deployBlockedReason = deploying ? ""
    : !name.trim() ? "이름을 입력해 주세요."
    : !description.trim() ? "설명을 입력해 주세요."
    : !systemPrompt.trim() ? "system prompt를 생성하거나 직접 입력해 주세요."
    : !escalationContact.trim()
      ? "2차 담당자(에스컬레이션)를 회원 검색으로 골라 주세요."
    : !isNameDeployable(nameAvailability)
      ? (nameAvailability === "unchecked"
        ? "이름 입력란에서 포커스를 빼면 사용 가능 여부를 확인해요."
        : nameConflict || "이름을 사용할 수 없어요.")
    : missingReasons.length > 0 ? "권한 신청 사유가 비어 있어요."
    : memoryErrors.length > 0 ? memoryErrors[0]
    : "";

  async function handleNameBlur() {
    const normalized = name.trim();
    if (!normalized) {
      setNameAvailability("unchecked");
      setNameConflict("");
      return;
    }
    const checkId = ++nameCheckIdRef.current;
    setNameAvailability("checking");
    try {
      const result = await checkAgentName(normalized);
      if (checkId !== nameCheckIdRef.current || !mountedRef.current) return;
      setNameAvailability(nameAvailabilityAfterCheck(result.available));
      setNameConflict(
        result.available ? "" : nameConflictMessage(result.reason),
      );
    } catch {
      if (checkId !== nameCheckIdRef.current || !mountedRef.current) return;
      setNameAvailability(nameAvailabilityAfterCheck(undefined));
      setNameConflict("이름 사용 가능 여부를 확인하지 못했어요.");
    }
  }

  async function handlePromptGenerate() {
    if (!description.trim()) return;
    const request = promptGateRef.current!.begin();
    setPromptGenerating(true);
    try {
      const response = await generatePrompt({
        name,
        description,
        tools: spec.tools,
      }, request.signal);
      if (promptGateRef.current?.canApply(request)) {
        setSystemPrompt(response.system_prompt);
        setPromptGenerated(true);
      }
    } catch (error) {
      if (!request.signal.aborted && mountedRef.current) {
        toastError(
          "system prompt를 생성하지 못했어요.",
          error instanceof Error ? error.message : "잠시 후 다시 시도해 주세요.",
        );
      }
    } finally {
      if (promptGateRef.current?.finish(request) && mountedRef.current) {
        setPromptGenerating(false);
      }
    }
  }

  async function loadExplore() {
    setExploreFiles([]);
    setExploreError("");
    setShowExplore(true);
    try {
      const response = await getScaffold(spec);
      setExploreFiles(response.files);
    } catch (error) {
      setExploreError(
        error instanceof Error ? error.message : "생성 코드를 불러오지 못했어요.",
      );
    }
  }

  function saveScaffoldDownload(
    blob: Blob,
    includesDevIdentity: boolean,
    excludedOperations: string[] = [],
  ) {
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = `${spec.name}.zip`;
    anchor.click();
    URL.revokeObjectURL(url);
    success(
      includesDevIdentity
        ? "코드와 dev 크리덴셜을 내려받았어요."
        : "크리덴셜 없이 코드를 내려받았어요.",
      `${spec.name}.zip`,
    );
    if (excludedOperations.length > 0) {
      // 고르는 시점 라벨과 **짝**이에요 — 조용히 줄이지 않으려면 두 번 보여야 해요.
      //
      // `durationMs: 0` 은 의도예요. 성공 토스트는 4.5초 뒤 사라지는데, 도구가 빠졌다는
      // 사실은 놓치면 로컬에서 "왜 이 도구가 없지" 로 되돌아와요. 실패(error)로 띄우지는
      // 않아요 — 다운로드는 성공했고, 붉은 알림은 ZIP 이 잘못된 것처럼 읽혀요.
      toast({
        title: "일부 도구는 ZIP 에 담기지 않았어요.",
        description: (
          `조회(READ)가 아닌 ${excludedOperations.length}개를 제외했어요: `
          + `${excludedOperations.join(", ")}. `
          + "로컬 실행에는 조회 도구만 담을 수 있어요 — 쓰기 도구는 배포한 agent 에서 쓰세요."
        ),
        tone: "info",
        durationMs: 0,
      });
    }
  }

  async function handleDownload() {
    const includeDevIdentity = shouldIncludeDevIdentity(selectedTools);
    try {
      const result = await attemptScaffoldDownload({
        spec,
        includeDevIdentity,
        download: downloadScaffold,
      });
      if (result.kind === "credential_unavailable") {
        setShowExplore(false);
        setCredentialFallback(result);
        toastError("dev 크리덴셜을 발급하지 못했어요.", result.reason);
        return;
      }
      saveScaffoldDownload(
        result.blob,
        result.includesDevIdentity,
        result.excludedOperations,
      );
    } catch (error) {
      toastError("코드를 내려받지 못했어요.", scaffoldDownloadFailureMessage(error));
    }
  }

  async function handleCodeOnlyDownload() {
    setCodeOnlyDownloading(true);
    try {
      const result = await attemptScaffoldDownload({
        spec,
        includeDevIdentity: false,
        download: downloadScaffold,
      });
      if (result.kind !== "downloaded") return;
      saveScaffoldDownload(result.blob, false);
      setCredentialFallback(null);
    } catch (error) {
      toastError("코드를 내려받지 못했어요.", scaffoldDownloadFailureMessage(error));
    } finally {
      if (mountedRef.current) setCodeOnlyDownloading(false);
    }
  }

  async function handleDeploy() {
    if (!canDeploy) return;
    setDeploying(true);
    setDeployError("");
    const toolRequests = buildAgentToolRequests(
      selectedTools,
      justifications,
    );
    // `starting=1` 은 "job 이 곧 생겨요" 신호예요. 권한 신청도 source meta에 실려
    // 서버 job이 소유하므로 READ/비-READ 여부와 무관하게 버튼 직후 이동해요.
    router.push("/catalog/requests?starting=1");
    try {
      const { files } = await getScaffold(spec);
      const { asset_id, version } = await uploadAgentFolder({
        name: spec.name,
        files: flattenTree(files),
        description: spec.description,
        deployment_source: "initializr",
        model: spec.model,
        escalation_contact: escalationContact,
        tool_requests: toolRequests,
      }, SESSION_PRINCIPAL);
      await startAgentDeploy(
        asset_id,
        version,
        SESSION_PRINCIPAL,
        undefined,
        crypto.randomUUID(),
      );
    } catch (error) {
      const message = error instanceof Error ? error.message : "배포에 실패했어요.";
      // 이미 '나의 요청' 으로 넘어간 뒤일 수 있어요. 그때 이 폼 상태는 아무도 안 보니
      // **toast 가 유일한 통보 경로**예요 — 그래서 toast 는 항상 띄워요.
      if (mountedRef.current) {
        if (error instanceof ApiError && error.status === 409) {
          setNameAvailability("unavailable");
          setNameConflict(error.message);
        }
        setDeployError(message);
      }
      toastError("Runtime 배포를 완료하지 못했어요.", message);
    } finally {
      if (mountedRef.current) setDeploying(false);
    }
  }

  function removeTool(toolId: string) {
    setSelectedTools((current) => current.filter((tool) => tool.id !== toolId));
    setJustifications((current) => {
      const next = { ...current };
      for (const key of Object.keys(next)) {
        if (key.startsWith(`${toolId}:`)) delete next[key];
      }
      return next;
    });
  }

  function toggleMemoryStrategy(strategy: MemoryStrategy) {
    setMemoryStrategies((current) => {
      const next = new Set(current);
      if (next.has(strategy)) {
        if (next.size === 1) return current;
        next.delete(strategy);
      } else {
        next.add(strategy);
      }
      return next;
    });
  }

  function removeSelectedOperation(
    tool: SelectedCatalogTool,
    operationId: string,
  ) {
    setSelectedTools((current) => current.map((item) => {
      if (item.id !== tool.id) return item;
      const selectedOperations = new Set(item.selectedOperations);
      selectedOperations.delete(operationId);
      return { ...item, selectedOperations };
    }));
    setJustifications((current) => {
      const next = { ...current };
      delete next[`${tool.id}:${operationId}`];
      return next;
    });
  }

  return (
    <div className="mx-auto max-w-6xl">
      <div className="mb-1 flex items-center gap-2.5">
        <span className="text-xl text-blue-600" aria-hidden>✦</span>
        <h1 className="text-2xl font-bold">Agent Initializr</h1>
        <span className="rounded bg-slate-100 px-2 py-0.5 text-xs font-medium text-slate-600">
          Strands · AgentCore Runtime
        </span>
      </div>
      <p className="mb-7 text-sm text-muted-foreground">
        화면에서 고른 설정을 Agora가 Strands 코드로 생성해 AgentCore Runtime에 배포합니다.
      </p>

      <div className="grid grid-cols-1 gap-7 lg:grid-cols-[minmax(0,1fr)_1px_minmax(0,1.08fr)]">
        <div className="space-y-7">
          <Section title="모델">
            <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
              {MODELS.map((option) => (
                <RadioCard
                  key={option.id}
                  active={model === option.id}
                  onClick={() => setModel(option.id)}
                  title={option.label}
                  note={`${option.note} · ${TEMPERATURE_NOTE[option.id]}`}
                />
              ))}
            </div>
          </Section>

          <Section title="에이전트 정보">
            <div>
              <div className="mb-1.5 flex items-center gap-2">
                <label htmlFor="strands-agent-name" className="text-xs font-medium text-muted-foreground">
                  이름
                </label>
                {nameAvailability === "checking" && (
                  <span className="text-xs text-muted-foreground">확인 중…</span>
                )}
                {nameAvailability === "unavailable" && (
                  <span className="text-xs font-medium text-red-700">{nameConflict}</span>
                )}
                {nameAvailability === "unknown" && (
                  <>
                    <span className="text-xs font-medium text-amber-700">{nameConflict}</span>
                    <button
                      type="button"
                      onClick={() => void handleNameBlur()}
                      className="text-xs font-medium text-blue-700 hover:underline"
                    >
                      다시 확인
                    </button>
                  </>
                )}
              </div>
              <Input
                id="strands-agent-name"
                value={name}
                disabled={promptGenerating}
                onChange={(event) => {
                  nameCheckIdRef.current += 1;
                  setName(event.target.value);
                  setNameAvailability("unchecked");
                  setNameConflict("");
                }}
                onBlur={() => void handleNameBlur()}
                placeholder="research-assistant"
                className={cn(
                  nameAvailability === "unavailable" && "border-red-500",
                  nameAvailability === "unknown" && "border-amber-500",
                )}
              />
            </div>
            <Field label="설명 (무엇을 하는 에이전트인가요?)">
              <textarea
                value={description}
                disabled={promptGenerating}
                onChange={(event) => setDescription(event.target.value)}
                rows={4}
                placeholder="이 에이전트가 어떤 일을 하는지 적어주세요. 이 설명으로 system prompt 초안을 만들어요."
                className="w-full resize-none rounded-lg border border-input bg-card px-3.5 py-2.5 text-sm placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-60"
              />
            </Field>
            <EscalationContactPicker
              value={escalationContact}
              onChange={setEscalationContact}
              initialMode="button"
            />
          </Section>

          <section>
            <div className="mb-3 flex items-center justify-between">
              <div className="flex items-center gap-2">
                <h2 className="text-sm font-semibold">Agora 카탈로그 도구</h2>
                <span className="rounded-full bg-accent px-2 py-0.5 text-xs text-muted-foreground">
                  {toolsLoading ? "…" : `${selectedTools.length}개`}
                </span>
              </div>
              <Button
                size="sm"
                variant="outline"
                disabled={promptGenerating}
                onClick={() => setShowTools(true)}
              >
                + 도구 추가
              </Button>
            </div>

            {selectedTools.length === 0 ? (
              <button
                type="button"
                disabled={promptGenerating}
                onClick={() => setShowTools(true)}
                className="w-full rounded-lg border border-dashed border-border bg-accent/20 px-4 py-8 text-sm text-muted-foreground hover:bg-accent/40 disabled:cursor-not-allowed disabled:opacity-60 disabled:hover:bg-accent/20"
              >
                카탈로그에서 Skill 또는 MCP operation을 추가하세요.
              </button>
            ) : (
              <div className="space-y-2">
                {selectedTools.map((tool) => (
                  <div key={tool.id} className="rounded-lg border border-border">
                    <div className="flex items-start gap-3 px-3 py-2.5">
                      <span className="min-w-0 flex-1">
                        <span className="flex items-center gap-2">
                          <span className="rounded bg-slate-100 px-1.5 py-0.5 text-[10px] font-medium">
                            {tool.kind === "mcp" ? "MCP" : "Skill"}
                          </span>
                          <span className="truncate font-mono text-sm font-medium">{tool.name}</span>
                          <span className="text-[11px] text-muted-foreground">{tool.owner}</span>
                        </span>
                        <span className="mt-0.5 block text-xs text-muted-foreground">
                          {tool.description}
                        </span>
                      </span>
                      <span className="flex shrink-0 items-center gap-3">
                        {tool.kind === "mcp" && (
                          <button
                            type="button"
                            disabled={promptGenerating}
                            onClick={() => setShowTools(true)}
                            className="text-xs text-blue-700 hover:underline disabled:cursor-not-allowed disabled:text-muted-foreground disabled:no-underline disabled:hover:no-underline"
                          >
                            도구 편집
                          </button>
                        )}
                        <button
                          type="button"
                          disabled={promptGenerating}
                          onClick={() => removeTool(tool.id)}
                          className="text-xs text-red-700 hover:underline disabled:cursor-not-allowed disabled:text-muted-foreground disabled:no-underline disabled:hover:no-underline"
                        >
                          제거
                        </button>
                      </span>
                    </div>
                    {tool.kind === "mcp" && (
                      <div className="border-t border-border px-3 py-2">
                        {visibleOperations(tool).map((operation) => (
                          <div
                            key={operation.id}
                            className="flex items-center gap-2 rounded px-1 py-1.5"
                          >
                            <span className="min-w-0 flex-1 truncate font-mono text-xs">
                              {operation.id}
                            </span>
                            <SensitivityBadge sensitivity={operation.sensitivity} />
                            <span className="text-[11px] text-muted-foreground">
                              {operationDeploymentApprovalLabel(
                                operation.sensitivity,
                              )}
                            </span>
                            {/* 배포 축과 **다른 축**이에요 — 배포는 승인 뒤 열리지만
                                로컬 ZIP 은 조회 도구만 담아요. 고른 뒤 알면 늦어요. */}
                            {operationLocalDownloadLabel(operation.sensitivity) && (
                              <span className="whitespace-nowrap rounded bg-amber-50 px-1.5 py-0.5 text-[11px] font-medium text-amber-800">
                                {operationLocalDownloadLabel(operation.sensitivity)}
                              </span>
                            )}
                            <button
                              type="button"
                              disabled={promptGenerating}
                              onClick={() => removeSelectedOperation(
                                tool,
                                operation.id,
                              )}
                              aria-label={`${operation.id} 제거`}
                              className="shrink-0 text-[11px] text-red-700 hover:underline disabled:cursor-not-allowed disabled:text-muted-foreground disabled:no-underline disabled:hover:no-underline"
                            >
                              제거
                            </button>
                          </div>
                        ))}
                        {tool.selectedOperations.size === 0 && (
                          <p className="mt-1 rounded bg-amber-50 px-2 py-1.5 text-xs text-amber-800">
                            {MCP_EMPTY_SELECTION_MESSAGE}
                          </p>
                        )}
                      </div>
                    )}
                  </div>
                ))}
              </div>
            )}
          </section>
        </div>

        <div className="hidden bg-border lg:block" aria-hidden />

        <div className="flex flex-col gap-7">
          {/* IH-101 temporary disablement: retain until strands-agents-tools
              extras permit bedrock-agentcore>=1.22.0.
          <SettingSection
            title="AgentCore 내장 도구"
            onInfo={() => setShowBuiltinInfo(true)}
          >
            <div className="grid gap-2 sm:grid-cols-2">
              {BUILTIN_TOOLS.map((tool) => {
                const selected = builtinTools.has(tool.id);
                return (
                  <button
                    key={tool.id}
                    type="button"
                    aria-pressed={selected}
                    onClick={() => toggleBuiltinTool(tool.id)}
                    className={cn(
                      "min-h-32 rounded-lg border px-4 py-3 text-left transition-colors",
                      selected
                        ? "border-teal-600 bg-teal-50 ring-1 ring-teal-600"
                        : "border-teal-200 bg-teal-50/30 hover:border-teal-400 hover:bg-teal-50/70",
                    )}
                  >
                    <span className="block text-sm font-semibold text-foreground">
                      {tool.name}
                    </span>
                    <span className="mt-1 block text-xs font-medium text-teal-800">
                      {tool.product}
                    </span>
                    <span className="mt-2 block text-xs leading-relaxed text-muted-foreground">
                      {tool.description}
                    </span>
                  </button>
                );
              })}
            </div>
          </SettingSection>
          */}

          <SettingSection
            title="기억 (Memory)"
            onInfo={() => setInfoKey("memory")}
          >
            <ChipGroup
              cols={2}
              value={memoryMode}
              onChange={(value) => setMemoryMode(value as MemoryMode)}
              options={[
                { v: "DISABLED", label: "사용 안 함", sub: "기본값" },
                { v: "MANAGED", label: "AWS 관리형", sub: "과금 대상" },
              ]}
            />
            {/* 고른 **뒤에** 알면 늦어요. 관리형은 로컬에서 두 이유로 못 돌아요 —
                `AGORA_MEMORY_ID` 는 배포 때만 생기고, `session_id` 는 AgentCore 헤더에서만
                와요. 예전에는 생성 `core.py` 가 둘 다 RuntimeError 로 세워서 로컬에서 대화
                자체가 안 됐어요. 이제는 기억만 빠진 채로 돌아가요(경고 로그). 그래서 문구도
                "고르지 마세요" 가 아니라 "배포하면 돼요" 예요 — 쓰기 도구와 같은 축이에요. */}
            {memoryMode === "MANAGED" ? (
              <p className="text-xs text-amber-700">
                <strong className="font-medium">기억은 로컬에서 시험할 수 없어요</strong> —
                내려받아 로컬에서 돌리면 대화는 되지만 기억만 빠진 채로 동작해요
                (`AGORA_MEMORY_ID`·세션 ID 가 배포 때만 생겨요). 쓰기 MCP 도구처럼{" "}
                <strong className="font-medium">카탈로그에 등록해 배포하면</strong>{" "}
                Playground 에서 그대로 시험할 수 있어요.
              </p>
            ) : (
              <p className="text-xs text-muted-foreground">
                기억을 쓰면 대화가 세션을 넘어 이어져요. 단, 기억은 배포한 agent 에서만
                동작해요 — 로컬 실행에서는 빠져요.
              </p>
            )}
            {memoryMode === "MANAGED" && (
              <div className="space-y-4 border-l-2 border-blue-200 pl-3">
                <fieldset>
                  <legend className="mb-2 text-xs font-medium text-muted-foreground">
                    장기 전략
                  </legend>
                  <div className="grid gap-2 sm:grid-cols-3">
                    {MEMORY_STRATEGIES.map((strategy) => {
                      const checked = memoryStrategies.has(strategy);
                      return (
                        <label
                          key={strategy}
                          className={cn(
                            "flex min-w-0 items-center gap-2 border px-2.5 py-2 text-xs",
                            checked
                              ? "border-blue-300 bg-blue-50 text-blue-800"
                              : "border-border bg-card text-muted-foreground",
                          )}
                        >
                          <input
                            type="checkbox"
                            checked={checked}
                            disabled={checked && memoryStrategies.size === 1}
                            onChange={() => toggleMemoryStrategy(strategy)}
                            className="h-4 w-4 shrink-0 accent-blue-600"
                          />
                          <span className="min-w-0 break-words font-mono">
                            {strategy}
                          </span>
                        </label>
                      );
                    })}
                  </div>
                </fieldset>
                <RangeRow
                  label="보존 기간"
                  value={memoryRetentionDays}
                  display={`${memoryRetentionDays}일`}
                  min={MEMORY_RETENTION_MIN}
                  max={MEMORY_RETENTION_MAX}
                  step={1}
                  onChange={setMemoryRetentionDays}
                />
                {memoryErrors.length > 0 && (
                  <ul
                    role="alert"
                    className="space-y-1 text-xs font-medium text-red-700"
                  >
                    {memoryErrors.map((message) => (
                      <li key={message}>{message}</li>
                    ))}
                  </ul>
                )}
              </div>
            )}
          </SettingSection>

          <SettingSection
            title="context 관리"
            onInfo={() => setInfoKey("context")}
          >
            <ChipGroup
              cols={2}
              value={contextStrategy}
              onChange={(value) => setContextStrategy(value as ContextStrategy)}
              options={[
                { v: "sliding_window", label: "Sliding window", sub: "최근 N개 유지" },
                { v: "none", label: "관리 안 함", sub: "자르지 않음" },
              ]}
            />
            {contextStrategy === "sliding_window" && (
              <RangeRow
                label="유지할 메시지 수 (window_size)"
                value={windowSize}
                display={String(windowSize)}
                min={SLIDING_WINDOW_MIN}
                max={100}
                step={5}
                onChange={setWindowSize}
              />
            )}
          </SettingSection>

          <SettingSection
            title="실행 상한"
            onInfo={() => setInfoKey("limits")}
          >
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
              <RangeRow
                label="max tokens"
                value={maxTokensK}
                display={`${maxTokensK}k`}
                min={4}
                max={64}
                step={1}
                onChange={setMaxTokensK}
              />
              <RangeRow
                label="max iterations"
                value={maxIterations}
                display={String(maxIterations)}
                min={1}
                max={30}
                step={1}
                onChange={setMaxIterations}
              />
            </div>
          </SettingSection>
          <section className="flex min-h-0 flex-1 flex-col">
            <div className="mb-2 flex items-center justify-between gap-3">
              <div className="flex items-center gap-2">
                <h2 className="text-sm font-semibold">system prompt</h2>
                {promptGenerated && (
                  <span className="text-xs text-muted-foreground">수정 가능</span>
                )}
              </div>
              <Button
                size="sm"
                variant="outline"
                disabled={!description.trim() || promptGenerating}
                onClick={() => void handlePromptGenerate()}
              >
                {promptGenerating ? (
                  <span className="flex items-center gap-1.5">
                    <span
                      aria-hidden
                      className="h-3 w-3 shrink-0 animate-spin rounded-full border-2 border-input border-t-foreground"
                    />
                    생성 중…
                  </span>
                ) : promptGenerated ? "다시 생성" : "자동 생성"}
              </Button>
            </div>
            <div className="relative flex min-h-0 flex-1 flex-col">
              <textarea
                value={systemPrompt}
                disabled={promptGenerating}
                onChange={(event) => setSystemPrompt(event.target.value)}
                rows={12}
                placeholder="자동 생성을 누르면 agent가 설명·도구를 보고 초안을 만들어요. 만든 뒤 자유롭게 고칠 수 있어요."
                className="h-full min-h-[18rem] w-full flex-1 resize-y rounded-lg border border-input bg-card px-3.5 py-3 font-mono text-xs leading-relaxed placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-60"
              />
              {promptGenerating && (
                <div
                  role="status"
                  aria-live="polite"
                  className="pointer-events-none absolute inset-0 flex flex-col items-center justify-center gap-2 rounded-lg bg-card/70"
                >
                  <span
                    aria-hidden
                    className="h-6 w-6 animate-spin rounded-full border-2 border-input border-t-foreground"
                  />
                  <span className="text-xs font-medium">system prompt를 생성하고 있어요</span>
                  <span className="text-[11px] text-muted-foreground">
                    모델 호출이라 15초쯤 걸려요. 끝날 때까지 설명·도구를 바꾸지 마세요.
                  </span>
                </div>
              )}
            </div>
          </section>
        </div>
      </div>

      <div className="mt-8 flex flex-wrap items-center gap-3 border-t border-border pt-5">
        <Button
          variant="outline"
          disabled={!systemPrompt.trim() || memoryErrors.length > 0}
          onClick={() => void loadExplore()}
        >
          생성 코드 미리보기
        </Button>
        <Button
          variant="outline"
          disabled={!systemPrompt.trim() || memoryErrors.length > 0}
          onClick={() => void handleDownload()}
        >
          코드 다운로드
        </Button>
        <Button
          disabled={!canDeploy}
          title={deployBlockedReason || undefined}
          onClick={() => void handleDeploy()}
        >
          {deploying ? "배포 진행 중…" : "Runtime에 배포"}
        </Button>
        {deployBlockedReason && (
          <p className="w-full text-xs text-amber-800">{deployBlockedReason}</p>
        )}
        {deployError && (
          <p className="w-full text-xs text-red-700">
            {deployError} 진행 상태는 나의 요청에서 확인해 주세요.
          </p>
        )}
        <span className="ml-auto text-xs text-muted-foreground">
          {MODELS.find((option) => option.id === model)?.label}
          {" · "}operation {operationCount}개
          {" · "}skill {skillCount}개
          {/* IH-101 re-enable after compatible extras:
          {" · "}내장 도구 {builtinTools.size}개
          */}
          {" · "}{memoryMode === "MANAGED" ? "관리형 기억" : "기억 없음"}
          {" · "}{contextStrategy === "sliding_window" ? `sliding ${windowSize}` : contextStrategy}
        </span>
      </div>

      {missingReasons.length > 0 && (
        <p className="mt-2 text-sm text-amber-800">
          신청 사유가 필요한 operation:{" "}
          {missingReasons.map((item) => item.operationId).join(", ")}
        </p>
      )}

      {showExplore && (
        <ExploreModal
          files={exploreFiles}
          error={exploreError}
          scaffoldName={spec.name}
          onDownload={() => void handleDownload()}
          onClose={() => setShowExplore(false)}
        />
      )}
      {showTools && (
        <AddCatalogToolsModal
          tools={catalogTools}
          selected={selectedTools}
          justifications={justifications}
          onConfirm={(tools, reasons) => {
            setSelectedTools(tools);
            setJustifications(reasons);
          }}
          onClose={() => setShowTools(false)}
        />
      )}
      {infoKey && (
        <Modal
          open
          size="md"
          title={INFO_CONTENT[infoKey].title}
          description={INFO_CONTENT[infoKey].caption}
          onClose={() => setInfoKey(null)}
        >
          <dl className="space-y-3 text-sm">
            {INFO_CONTENT[infoKey].items.map((item) => (
              <div key={item.term}>
                <dt className="font-semibold text-foreground">{item.term}</dt>
                <dd className="mt-0.5 text-muted-foreground">{item.desc}</dd>
              </div>
            ))}
          </dl>
        </Modal>
      )}
      <Modal
        open={Boolean(credentialFallback)}
        size="md"
        title="dev 크리덴셜을 발급하지 못했어요"
        description={credentialFallback?.reason}
        busy={codeOnlyDownloading}
        onClose={() => setCredentialFallback(null)}
        footer={(
          <>
            <Button
              variant="ghost"
              disabled={codeOnlyDownloading}
              onClick={() => setCredentialFallback(null)}
            >
              취소
            </Button>
            <Button
              disabled={codeOnlyDownloading}
              onClick={() => void handleCodeOnlyDownload()}
            >
              <Icon name="download" size={15} />
              {codeOnlyDownloading
                ? "다운로드 중…"
                : "크리덴셜 없이 코드만 다운로드"}
            </Button>
          </>
        )}
      >
        <div className="space-y-3 text-sm">
          <p className="text-muted-foreground">
            코드만 받으면 ZIP에 `.env`와 dev 크리덴셜이 들어가지 않아요.
            로컬 실행 전 README와 `.env.example`의 인증 절차를 완료해야 MCP를 호출할 수 있어요.
          </p>
          {credentialFallback?.actionUrl && (
            <a
              href={credentialFallback.actionUrl}
              className="inline-flex font-medium text-primary underline underline-offset-4"
            >
              거버넌스에서 권한 확인
            </a>
          )}
        </div>
      </Modal>
      {/* IH-101 temporary disablement: retain this information modal until
          strands-agents-tools extras permit bedrock-agentcore>=1.22.0.
      <Modal
        open={showBuiltinInfo}
        size="md"
        title="AgentCore 내장 도구"
        description="내장 도구 사용은 agent별 리소스와 사후 관측 기록으로 추적해요."
        onClose={() => setShowBuiltinInfo(false)}
      >
        <dl className="space-y-3 text-sm">
          <div>
            <dt className="font-semibold text-foreground">기록되는 정보</dt>
            <dd className="mt-0.5 text-muted-foreground">
              세션 메트릭과 사용량 로그가 리소스별로 기록되고, Browser는 세션 녹화도 항상 저장해요.
            </dd>
          </div>
          <div>
            <dt className="font-semibold text-foreground">사후 추적</dt>
            <dd className="mt-0.5 text-muted-foreground">
              사용 흔적을 확인하는 방식이라 호출 자체를 막지는 않아요.
            </dd>
          </div>
          <div>
            <dt className="font-semibold text-foreground">공개 인터넷 연결</dt>
            <dd className="mt-0.5 text-muted-foreground">
              두 도구 모두 networkMode: PUBLIC이라 공개 인터넷으로 나갈 수 있어요.
              이 트래픽은 Agora Gateway와 Cedar 인가를 경유하지 않아 도구 단위 사전 차단과 감사가 적용되지 않아요.
            </dd>
          </div>
          <div>
            <dt className="font-semibold text-foreground">생성 후 변경 제한</dt>
            <dd className="mt-0.5 text-muted-foreground">
              이 네트워크 선택은 리소스를 만든 뒤 바꿀 수 없어요.
              Code Interpreter에는 UpdateCodeInterpreter API가 없고 Browser는 SANDBOX 모드를 지원하지 않아요.
            </dd>
          </div>
          <div>
            <dt className="font-semibold text-foreground">Browser 녹화 주의</dt>
            <dd className="mt-0.5 text-muted-foreground">
              브라우저 세션 녹화는 화면 조작, 입력 폼, 네트워크 이벤트를 저장해요.
              민감정보를 입력하는 사이트에는 사용하지 마세요.
            </dd>
          </div>
        </dl>
      </Modal>
      */}
    </div>
  );
}
