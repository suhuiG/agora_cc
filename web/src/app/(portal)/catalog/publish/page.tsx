"use client";

import { useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import useSWR from "swr";
import {
  publishSource,
  registerMcp,
  connectTestMcp,
  connectTestAgent,
  registerAgent,
  uploadMcpFolder,
  startMcpDeploy,
  previewMcpDeployTools,
  uploadAgentFolder,
  startAgentDeploy,
  ApiError,
  SESSION_PRINCIPAL,
  type SourceFile,
  type AgentConnectTestResult,
  type McpToolPreview,
} from "@/lib/api";
import { getAuthSession, type AuthSession } from "@/lib/auth-client";
import { collectFolderFiles, isCredentialPath } from "@/lib/folderUpload";
import { mcpEndpointSchemeProblem } from "@/lib/mcpEndpoint";
import { escalationContactProblem } from "@/lib/responsibilityContacts";
import { SEMVER_RE } from "@/lib/semver";
import { MetadataStep } from "./_components/MetadataStep";
import { useToast } from "@/components/ui/toast";
import { StepOne } from "./_components/StepOne";
import { StepTwo } from "./_components/StepTwo";
import { McpDeployToolsStep } from "./_components/McpDeployToolsStep";
import { WizardProgress } from "./_components/WizardProgress";
import {
  newRow,
  parseFrontmatter,
  unsafePathReason,
  type FileRow,
  type PublishForm,
  type WizardChoice,
} from "./_components/types";

export default function PublishPage() {
  const router = useRouter();

  const [step, setStep] = useState<1 | 2 | 3 | 4>(1);
  const [choice, setChoice] = useState<WizardChoice | null>(null);

  // Step 2 — source 모드 파일 에디터
  const [files, setFiles] = useState<FileRow[]>([]);
  // Step 2 — reference 모드 입력
  const [mcpEndpoint, setMcpEndpoint] = useState("");
  const [connectVerified, setConnectVerified] = useState(false);
  const [connecting, setConnecting] = useState(false);
  const [mcpTools, setMcpTools] = useState<{ name: string; description: string }[]>([]);

  // Step 2 — agent 모드 입력 (카드 업로드 + 도메인 연결, 둘 다 선택적)
  const [agentCardText, setAgentCardText] = useState("");   // agent-card.json 본문(붙여넣기/업로드)
  const [agentDomain, setAgentDomain] = useState("");       // 도메인/agent-card URL
  const [agentConnecting, setAgentConnecting] = useState(false);
  const [agentCard, setAgentCard] = useState<AgentConnectTestResult | null>(null);  // connect 결과
  const [agentDomainError, setAgentDomainError] = useState("");

  // Step 3 — 공통 메타데이터
  // owner_user는 폼에서 받지 않아요(§11.6). 등록 주체는 SESSION_PRINCIPAL에서 파생되고,
  // 사용자 기능 연동 전까지 기본값(myname)을 써요. 상세 화면에서만 소유자로 표시돼요.
  const [form, setForm] = useState<PublishForm>({
    name: "",
    description: "",
    version: "1.0.0",
    category: "",
    owner_team: "myteam",
    escalation_contact: "",
    tags: "",
    changelog: "",
  });
  const { data: authSession } = useSWR<AuthSession>(
    "/api/auth/session",
    () => getAuthSession(),
  );
  const idpTeam = authSession?.team?.trim() ?? "";
  const effectiveOwnerTeam = idpTeam || form.owner_team;
  // 1차 담당자는 서버가 principal 에서 파생해요 (CA-29). 이 값은 **표시·검증 전용**이고
  // 요청 본문에 담지 않아요 — 서버가 클라이언트 입력을 무시하니까요.
  const ownerContact = authSession?.email?.trim().toLowerCase() ?? "";
  // 폴더 업로드 시 제외된 비텍스트 파일 경고(비차단).
  const [skippedFiles, setSkippedFiles] = useState<string[]>([]);
  // 크리덴셜이라 빼둔 파일 — skippedFiles(비텍스트)와 섞지 않아요. 사용자에게
  // 보여줄 이유가 다르고, 조용히 빼면 뭐가 안 올라갔는지 알 수 없어요.
  const [excludedSecrets, setExcludedSecrets] = useState<string[]>([]);

  // Step 2/3 — deploy 모드(MCP): 선택한 소스 파일.
  const [deployFiles, setDeployFiles] = useState<SourceFile[]>([]);

  // Step 4 (deploy 모드): 업로드 후 tool 미리보기.
  const [deployAsset, setDeployAsset] = useState<{ asset_id: string; version: string } | null>(null);
  const [previewTools, setPreviewTools] = useState<McpToolPreview[]>([]);
  const [selectedMcpTools, setSelectedMcpTools] = useState<Set<string>>(new Set());
  const [previewLoading, setPreviewLoading] = useState(false);
  const [previewError, setPreviewError] = useState("");

  // Step 2/3 — agent-deploy 모드: 선택한 소스 파일.
  // MCP deploy와 별도 상태로 관리해 기존 deploy 흐름에 영향이 없어요.
  const [agentDeployFiles, setAgentDeployFiles] = useState<SourceFile[]>([]);

  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");
  const { success, error: toastError } = useToast();

  // 등록 성공 알림. 6개 모드가 모두 /catalog/requests 로 이동하는데, 예전엔 아무 알림 없이
  // 화면만 바뀌어서 "됐는지" 확인할 지점이 없었어요. 토스트 provider 가 루트에 있어서
  // 이동 후에도 알림이 살아 있어요.
  function notifyPublished() {
    success(
      "등록 요청을 접수했어요.",
      "심사 진행 상황은 이 화면(나의 요청)에서 확인할 수 있어요.",
    );
  }

  function updateForm(key: keyof typeof form, value: string) {
    setForm((prev) => ({ ...prev, [key]: value }));
  }

  function pickChoice(next: WizardChoice) {
    setChoice(next);
    setError("");
    setSkippedFiles([]); // 모드 전환 시 이전 스킵 경고 초기화
    setExcludedSecrets([]);
    setForm((prev) => ({
      ...prev,
      version: prev.version || "1.0.0",
    }));
    if (next.mode === "source") {
      // 필수 파일 한 줄을 미리 채워둬서 빠뜨리지 않게 해요.
      setFiles([newRow(next.requiredFile)]);
    } else {
      setFiles([]);
    }
    // agent 모드 입력 초기화.
    setAgentCardText("");
    setAgentDomain("");
    setAgentCard(null);
    setAgentDomainError("");
    // deploy 모드 입력 초기화.
    setDeployFiles([]);
    setSelectedMcpTools(new Set());
    // agent-deploy 모드 입력 초기화.
    setAgentDeployFiles([]);
    setStep(2);
  }

  // --- Step 2 파일 에디터 핸들러 -------------------------------------------
  function updateFile(id: string, patch: Partial<Pick<FileRow, "path" | "content">>) {
    setFiles((prev) => prev.map((f) => (f.id === id ? { ...f, ...patch } : f)));
  }
  function addFile() {
    setFiles((prev) => [...prev, newRow()]);
  }
  function removeFile(id: string) {
    setFiles((prev) => prev.filter((f) => f.id !== id));
  }
  // 개별 파일 선택("파일 불러오기") — 폴더 선택과 달리 collectFolderFiles 를 안 지나요.
  // 그래서 크리덴셜 제외를 여기서 따로 걸어요. 이 경로만 뚫려 있으면 `.env` 를 손으로
  // 골라 등록할 수 있고, 서버가 422 로 막긴 하지만 사용자는 이유를 늦게 알게 돼요.
  async function onPickFile(e: React.ChangeEvent<HTMLInputElement>) {
    const fileList = e.target.files;
    if (!fileList || fileList.length === 0) return;
    const added: FileRow[] = [];
    const secrets: string[] = [];
    for (const f of Array.from(fileList)) {
      if (isCredentialPath(f.webkitRelativePath || f.name)) {
        secrets.push(f.name);
        continue;
      }
      const text = await f.text();
      added.push(newRow(f.name, text));
    }
    setFiles((prev) => [...prev, ...added]);
    setExcludedSecrets(secrets);
    // 같은 파일을 다시 고를 수 있도록 input 을 비워요.
    e.target.value = "";
  }

  // Skill 폴더 통째 선택 → 텍스트 파일만 FileRow로, root SKILL.md frontmatter로 메타 자동채움.
  async function onPickFolder(e: React.ChangeEvent<HTMLInputElement>) {
    const fileList = e.target.files;
    if (!fileList || fileList.length === 0) return;
    const { files: collected, skipped, excludedSecrets: secrets } =
      await collectFolderFiles(fileList);
    const rows = collected.map((f) => newRow(f.path, f.content));
    rows.sort((a, b) =>
      a.path === "SKILL.md" ? -1 : b.path === "SKILL.md" ? 1 : a.path.localeCompare(b.path),
    );
    setFiles(rows);
    setSkippedFiles(skipped);
    setExcludedSecrets(secrets);
    const skillMd = collected.find((f) => f.path === "SKILL.md")?.content ?? "";
    if (skillMd) {
      const fm = parseFrontmatter(skillMd);
      setForm((prev) => ({
        ...prev,
        name: fm.name ?? prev.name,
        description: fm.description ?? prev.description,
        version: fm.version && SEMVER_RE.test(fm.version) ? fm.version : prev.version,
        changelog: fm.changelog ?? prev.changelog,
      }));
    }
    e.target.value = "";
  }

  // 배포형 MCP 폴더 선택 → SourceFile[] 수집(에디터 없이 파일 목록만). skill과 같은 필터·정규화.
  async function onPickDeployFolder(e: React.ChangeEvent<HTMLInputElement>) {
    const fileList = e.target.files;
    if (!fileList || fileList.length === 0) return;
    const { files: collected, skipped, excludedSecrets: secrets } =
      await collectFolderFiles(fileList);
    collected.sort((a, b) => a.path.localeCompare(b.path));
    setDeployFiles(collected);
    setSkippedFiles(skipped);
    setExcludedSecrets(secrets);
    e.target.value = "";
  }

  // 배포형 agent 폴더 선택 → SourceFile[] 수집. agent-card.json 필수 확인은 step2Valid에서.
  async function onPickAgentDeployFolder(e: React.ChangeEvent<HTMLInputElement>) {
    const fileList = e.target.files;
    if (!fileList || fileList.length === 0) return;
    const { files: collected, skipped, excludedSecrets: secrets } =
      await collectFolderFiles(fileList);
    collected.sort((a, b) => a.path.localeCompare(b.path));
    setAgentDeployFiles(collected);
    setSkippedFiles(skipped);
    setExcludedSecrets(secrets);
    e.target.value = "";
  }

  // --- Step 2 → 3 진행 가능 여부 -------------------------------------------
  function step2Valid(): boolean {
    if (!choice) return false;
    if (choice.mode === "reference") {
      return mcpEndpoint.trim().length > 0 && connectVerified;
    }
    if (choice.mode === "agent-domain") {
      return agentCard !== null;          // 도메인 connect 검증 완료
    }
    if (choice.mode === "agent-json") {
      return agentCardValid();            // 카드 JSON 유효 + name
    }
    if (choice.mode === "deploy") {
      // 폴더에서 최소 1개 텍스트 파일 수집 + 모든 경로 안전.
      return (
        deployFiles.length > 0 &&
        deployFiles.every((f) => unsafePathReason(f.path) === null)
      );
    }
    if (choice.mode === "agent-deploy") {
      // agent-card.json 필수 + 모든 경로 안전.
      const hasCard = agentDeployFiles.some((f) => f.path === "agent-card.json");
      return (
        agentDeployFiles.length > 0 &&
        hasCard &&
        agentDeployFiles.every((f) => unsafePathReason(f.path) === null)
      );
    }
    // source: 필수 파일이 비어있지 않게 존재 + 모든 경로 안전
    const hasRequired = files.some(
      (f) => f.path.trim() === choice.requiredFile && f.content.trim().length > 0,
    );
    const allSafe = files.every((f) => unsafePathReason(f.path) === null);
    return hasRequired && allSafe;
  }

  // --- Step 3 제출 ----------------------------------------------------------
  function tagList(): string[] {
    return form.tags
      .split(",")
      .map((t) => t.trim())
      .filter(Boolean);
  }

  /** ApiError 를 상황별 친절한 한국어 메시지로 변환해요. */
  function friendlyError(err: unknown): string {
    if (err instanceof ApiError) {
      if (err.detail?.remediation) {
        // 서버가 구조화된 안내를 주면 그걸 그대로 보여줘요 — 중복 등록 409(CA-33),
        // Gateway 연결 실패 502(CA-32) 처럼 "무엇을 하면 되는지"가 응답에 실려 있어요.
        // status 로 좁히지 않는 이유는, remediation 을 주는 응답이면 그 안내가 항상
        // 사용자에게 필요하기 때문이에요.
        const id = err.detail.record_id ? `\n(레코드 ID: ${err.detail.record_id})` : "";
        return `${err.message}\n${err.detail.remediation}${id}`;
      }
      if (err.status === 409) {
        // 백엔드 메시지(불완전 업로드 vs 중복 버전)를 그대로 살리되, 기본 안내를 덧붙여요.
        const lead =
          "이미 같은 버전이 있어요 (불변 버전이라 덮어쓸 수 없어요). 버전을 올리거나, 버전 칸을 비워서 자동으로 다음 버전을 매기게 해주세요.";
        return err.message && err.message !== "Conflict"
          ? `${lead}\n(서버: ${err.message})`
          : lead;
      }
      if (err.status === 422) {
        return `입력을 확인해 주세요: ${err.message}`;
      }
      if (err.status === 404) {
        return `대상을 찾지 못했어요 (404): ${err.message}`;
      }
      return `퍼블리시에 실패했어요 (${err.status}): ${err.message}`;
    }
    if (err instanceof Error) return err.message;
    return "알 수 없는 오류가 발생했어요.";
  }

  // --- Step 2 연결형 MCP connect 테스트 -------------------------------------
  async function handleConnectTest() {
    setError("");
    // CA-31: `https` 가 아니면 connect 조회 전에 막아요. AgentCore Gateway 가 MCP server
    // target endpoint 에 https 만 받아서, 예전엔 도구 목록까지 보여준 뒤 퍼블리시 마지막
    // 단계에서 502 로 떨어졌고 원인(스킴)이 어디에도 안 나왔어요.
    const schemeProblem = mcpEndpointSchemeProblem(mcpEndpoint);
    if (schemeProblem) {
      setError(schemeProblem);
      toastError("이 endpoint 는 등록할 수 없어요.", schemeProblem);
      return;
    }
    setConnecting(true);
    try {
      const res = await connectTestMcp(mcpEndpoint.trim());
      setConnectVerified(true);
      setMcpTools(res.tools);
      // 이름·설명 자동 채움(비어있을 때만, 사용자 입력 보존).
      // 설명은 서버가 준 긴 영문 instructions 대신, 기본 언어(한글) 기준 간단 템플릿으로.
      const serverName = res.name || "MCP";
      const autoDesc = `${serverName} MCP 서버 · 도구 ${res.tools.length}개 제공`;
      success("MCP 에 연결했어요.", `${serverName} · 도구 ${res.tools.length}개를 찾았어요.`);
      setForm((prev) => ({
        ...prev,
        name: prev.name || res.name || "",
        description: prev.description || autoDesc,
      }));
    } catch (err) {
      setConnectVerified(false);
      setMcpTools([]);
      // setError 는 3단계(MetadataStep)에서만 렌더돼서 2단계에선 안 보여요 — 토스트로 알려요.
      const message = friendlyError(err);
      setError(message);
      toastError("MCP endpoint 에 연결하지 못했어요.", message);
    } finally {
      setConnecting(false);
    }
  }

  // --- Step 2 agent 도메인 connect 테스트 -----------------------------------
  async function handleAgentConnect() {
    setAgentDomainError("");
    setAgentConnecting(true);
    try {
      const res = await connectTestAgent(agentDomain.trim());
      setAgentCard(res);
      success("에이전트 도메인에 연결했어요.", res.name || agentDomain.trim());
      // 이름·설명 자동 채움(비어있을 때만).
      setForm((prev) => ({
        ...prev,
        name: prev.name || res.name || "",
        description: prev.description || res.description || "",
      }));
    } catch (err) {
      setAgentCard(null);
      const message = friendlyError(err);
      setAgentDomainError(message);
      toastError("에이전트 도메인에 연결하지 못했어요.", message);
    } finally {
      setAgentConnecting(false);
    }
  }

  // agent-card.json 파일 업로드 → 텍스트로 읽고 name/description 자동 채움.
  async function onPickAgentCard(e: React.ChangeEvent<HTMLInputElement>) {
    const f = e.target.files?.[0];
    if (!f) return;
    const text = await f.text();
    setAgentCardText(text);
    applyAgentCardMeta(text);
    e.target.value = "";
  }

  // 카드 텍스트(JSON)에서 name/description을 폼에 자동 채움(비어있을 때만).
  function applyAgentCardMeta(text: string) {
    try {
      const card = JSON.parse(text) as { name?: string; description?: string };
      setForm((prev) => ({
        ...prev,
        name: prev.name || card.name || "",
        description: prev.description || card.description || "",
      }));
    } catch {
      /* 파싱 실패는 제출 시 검증에서 잡아요 */
    }
  }

  // 카드 텍스트가 유효한 JSON + name 있는지 (제출 가능 판단용).
  function agentCardValid(): boolean {
    if (!agentCardText.trim()) return false;
    try {
      const c = JSON.parse(agentCardText) as { name?: unknown };
      return typeof c.name === "string" && c.name.length > 0;
    } catch {
      return false;
    }
  }

  // --- 배포형 MCP: 업로드(finalize)만 하고 Step 4(tool 미리보기)로 진입해요. -----
  // 실제 배포 시작은 Step 4의 "배포 시작"에서 startMcpDeploy로 해요.
  // 예외는 handleSubmit이 잡아 friendlyError로 보여줘요(throw 그대로 전파).
  async function runDeployUpload() {
    const { asset_id, version } = await uploadMcpFolder(
      {
        name: form.name.trim(),
        files: deployFiles,
        description: form.description,
        owner_team: effectiveOwnerTeam,
        escalation_contact: form.escalation_contact.trim(),
        tags: tagList(),
        category: form.category,
      },
      SESSION_PRINCIPAL,
    );
    setDeployAsset({ asset_id, version });
    setStep(4);
    setPreviewLoading(true);
    setPreviewError("");
    try {
      const r = await previewMcpDeployTools(asset_id, version, SESSION_PRINCIPAL);
      setPreviewTools(r.tools);
      setSelectedMcpTools(new Set(r.tools.map((tool) => tool.name)));
    } catch {
      setPreviewError("tool 미리보기를 불러오지 못했어요.");
    } finally {
      setPreviewLoading(false);
    }
  }

  // Step 4 "배포 시작" — 업로드해 둔 소스로 배포 job을 시작하고 My Requests로 이동해요.
  async function runDeployStart(selectedTools: string[]) {
    if (!deployAsset) return;
    setSubmitting(true);
    try {
      await startMcpDeploy(
        deployAsset.asset_id,
        deployAsset.version,
        selectedTools,
        SESSION_PRINCIPAL,
      );
      notifyPublished();
      router.push("/catalog/requests");
    } catch (err) {
      setError(friendlyError(err));
      toastError("배포를 시작하지 못했어요.", friendlyError(err));
      setSubmitting(false);
    }
  }

  function toggleMcpTool(name: string) {
    setSelectedMcpTools((current) => {
      const next = new Set(current);
      if (next.has(name)) next.delete(name);
      else next.add(name);
      return next;
    });
  }

  // --- 배포형 agent: 폴더 업로드 → 배포 job 시작 → 나의 요청으로 이동 ----------
  // Agent-tool 인가는 관리자 매트릭스에서만 관리해요(등록 주체는 제안하지 않아요).
  // 예외는 handleSubmit이 잡아 friendlyError로 보여줘요(throw 그대로 전파).
  async function runAgentDeploy() {
    const { asset_id, version } = await uploadAgentFolder(
      {
        name: form.name.trim(),
        files: agentDeployFiles,
        description: form.description,
        owner_team: effectiveOwnerTeam,
        escalation_contact: form.escalation_contact.trim(),
        tags: tagList(),
        category: form.category,
      },
      SESSION_PRINCIPAL,
    );
    await startAgentDeploy(asset_id, version, SESSION_PRINCIPAL);
    success(
      "배포 요청을 접수했어요.",
      "빌드·배포는 수 분 걸려요. 진행 상황은 나의 요청에서 확인할 수 있어요.",
    );
    router.push("/catalog/requests");
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!choice) return;
    setError("");

    // --- 클라이언트 사전 검증 ---
    if (!form.name.trim()) {
      setError("이름을 입력해 주세요.");
      return;
    }
    // 담당자 계약(CA-29 · ADR-0069). 서버가 정본이고 여기 검사는 왕복을 줄이는 것뿐이에요.
    // 6개 submit 지점이 모두 이 함수를 지나니 한 곳에서 새지 않아요.
    const contactProblem = escalationContactProblem(
      form.escalation_contact,
      ownerContact,
    );
    if (contactProblem) {
      setError(contactProblem);
      return;
    }

    if (choice.mode === "source") {
      if (!SEMVER_RE.test(form.version.trim())) {
        setError("버전은 semver 형식이어야 해요 (예: 1.0.0).");
        return;
      }
      // 경로 안전성 한 번 더 확인
      for (const f of files) {
        const reason = unsafePathReason(f.path);
        if (reason) {
          setError(`파일 "${f.path || "(이름 없음)"}": ${reason}`);
          return;
        }
      }
      const hasRequired = files.some(
        (f) =>
          f.path.trim() === choice.requiredFile && f.content.trim().length > 0,
      );
      if (!hasRequired) {
        setError(
          `필수 파일 ${choice.requiredFile} 이(가) 비어 있어요. 내용을 채워 주세요.`,
        );
        return;
      }
    } else if (choice.mode === "agent-domain") {
      if (!agentCard) {
        setError("도메인을 연결해 주세요 (connect 테스트 필요).");
        return;
      }
    } else if (choice.mode === "agent-json") {
      if (!agentCardValid()) {
        setError("agent-card.json이 유효한 JSON이 아니거나 name이 없어요.");
        return;
      }
    } else if (choice.mode === "deploy") {
      if (deployFiles.length === 0) {
        setError("배포할 MCP 소스 폴더를 선택해 주세요.");
        return;
      }
      for (const f of deployFiles) {
        const reason = unsafePathReason(f.path);
        if (reason) {
          setError(`파일 "${f.path || "(이름 없음)"}": ${reason}`);
          return;
        }
      }
    } else if (choice.mode === "agent-deploy") {
      if (agentDeployFiles.length === 0) {
        setError("배포할 agent 소스 폴더를 선택해 주세요.");
        return;
      }
      const hasCard = agentDeployFiles.some((f) => f.path === "agent-card.json");
      if (!hasCard) {
        setError("agent-card.json 파일이 폴더 root에 있어야 해요.");
        return;
      }
      for (const f of agentDeployFiles) {
        const reason = unsafePathReason(f.path);
        if (reason) {
          setError(`파일 "${f.path || "(이름 없음)"}": ${reason}`);
          return;
        }
      }
    } else {
      // reference
      if (!mcpEndpoint.trim()) {
        setError("MCP endpoint URL 을 입력해 주세요.");
        return;
      }
      const schemeProblem = mcpEndpointSchemeProblem(mcpEndpoint);
      if (schemeProblem) {
        setError(schemeProblem);
        return;
      }
    }

    setSubmitting(true);
    try {
      if (choice.mode === "source") {
        const sourceFiles: SourceFile[] = files.map((f) => ({
          path: f.path.trim(),
          content: f.content,
        }));
        await publishSource(
          {
            asset_type: choice.assetType,
            name: form.name.trim(),
            version: form.version.trim() || undefined,
            files: sourceFiles,
            description: form.description,
            owner_team: effectiveOwnerTeam,
            escalation_contact: form.escalation_contact.trim(),
            tags: tagList(),
            category: form.category,
            changelog: form.changelog,
          },
          SESSION_PRINCIPAL,
        );
        notifyPublished();
        router.push("/catalog/requests");
      } else if (choice.mode === "agent-json") {
        // A2A agent — agent-card.json 카드 업로드(소스 경로). 심사 진행은 나의 요청에서 확인해요.
        await publishSource(
          {
            asset_type: "agent",
            name: form.name.trim(),
            version: form.version.trim() || undefined,
            files: [{ path: "agent-card.json", content: agentCardText }],
            description: form.description,
            owner_team: effectiveOwnerTeam,
            escalation_contact: form.escalation_contact.trim(),
            tags: tagList(),
            category: form.category,
            changelog: form.changelog,
          },
          SESSION_PRINCIPAL,
        );
        notifyPublished();
        router.push("/catalog/requests");
      } else if (choice.mode === "agent-domain") {
        // A2A agent — 도메인 연결(reference 경로). 등록 직후 심사 진행은 나의 요청에서 확인해요.
        await registerAgent({
          endpoint: agentDomain.trim(),
          name: form.name.trim(),
          description: form.description,
          owner_team: effectiveOwnerTeam,
          escalation_contact: form.escalation_contact.trim(),
          tags: tagList(),
          category: form.category,
        });
        notifyPublished();
        router.push("/catalog/requests");
      } else if (choice.mode === "deploy") {
        // 배포형 MCP — 폴더 업로드 후 Step 4(tool 미리보기)로. 배포 시작은 Step 4에서.
        await runDeployUpload();
        setSubmitting(false);
      } else if (choice.mode === "agent-deploy") {
        // 배포형 agent — 폴더 업로드 → 배포 job 시작 후 My Requests로 이동.
        await runAgentDeploy();
      } else {
        // 연결형 MCP — 중앙 호스팅 등록
        await registerMcp({
          mode: "connect",
          name: form.name.trim(),
          endpoint: mcpEndpoint.trim(),
          description: form.description,
          owner_team: effectiveOwnerTeam,
          escalation_contact: form.escalation_contact.trim(),
          tags: tagList(),
          category: form.category,
        });
        notifyPublished();
        router.push("/catalog/requests");
      }
    } catch (err: unknown) {
      // 사용자 입력은 그대로 유지하고, 폼에 머물러 다시 시도할 수 있게 해요.
      const message = friendlyError(err);
      setError(message);
      toastError("등록하지 못했어요.", message);
      setSubmitting(false);
    }
  }

  return (
    <div className="max-w-2xl mx-auto">
      <Link
        href="/catalog/browse"
        className="text-sm text-blue-600 hover:underline mb-4 inline-block"
      >
        ← 카탈로그
      </Link>
      <h1 className="text-2xl font-bold mb-1">퍼블리시</h1>
      <p className="mb-4 text-sm text-slate-400">
        {choice && (
          <span className="text-slate-500">{choice.label}</span>
        )}
      </p>
      {choice && (
        <WizardProgress
          current={step}
          total={choice.mode === "deploy" ? 4 : 3}
        />
      )}

      {step === 1 && <StepOne choice={choice} onPick={pickChoice} />}

      {step === 2 && choice && (
        <StepTwo
          choice={choice}
          files={files}
          skippedFiles={skippedFiles}
          excludedSecrets={excludedSecrets}
          mcpEndpoint={mcpEndpoint}
          connectVerified={connectVerified}
          connecting={connecting}
          mcpTools={mcpTools}
          deployFiles={deployFiles}
          agentDeployFiles={agentDeployFiles}
          agentCardText={agentCardText}
          agentCardValid={agentCardValid()}
          agentDomain={agentDomain}
          agentConnecting={agentConnecting}
          agentCard={agentCard}
          agentDomainError={agentDomainError}
          canContinue={step2Valid()}
          onPickFolder={onPickFolder}
          onPickFile={onPickFile}
          onUpdateFile={updateFile}
          onAddFile={addFile}
          onRemoveFile={removeFile}
          onMcpEndpointChange={(value) => {
            setMcpEndpoint(value);
            setConnectVerified(false);
            setMcpTools([]);
          }}
          onMcpConnect={handleConnectTest}
          onPickDeployFolder={onPickDeployFolder}
          onPickAgentDeployFolder={onPickAgentDeployFolder}
          onAgentCardTextChange={(value) => {
            setAgentCardText(value);
            applyAgentCardMeta(value);
          }}
          onPickAgentCard={onPickAgentCard}
          onAgentDomainChange={(value) => {
            setAgentDomain(value);
            setAgentCard(null);
            setAgentDomainError("");
          }}
          onAgentConnect={handleAgentConnect}
          onPrevious={() => setStep(1)}
          onNext={() => setStep(3)}
        />
      )}

      {step === 3 && choice && (
        <MetadataStep
          choice={choice}
          mode={choice.mode}
          form={{ ...form, owner_team: effectiveOwnerTeam }}
          error={error}
          submitting={submitting}
          ownerTeamReadOnly={idpTeam.length > 0}
          ownerContact={ownerContact}
          onUpdateForm={updateForm}
          onPrevious={() => setStep(2)}
          onSubmit={handleSubmit}
        />
      )}

      {step === 4 && choice?.mode === "deploy" && (
        <McpDeployToolsStep
          loading={previewLoading}
          tools={previewTools}
          error={previewError}
          submitting={submitting}
          selected={selectedMcpTools}
          onToggle={toggleMcpTool}
          onPrevious={() => setStep(3)}
          onDeploy={runDeployStart}
        />
      )}
    </div>
  );
}
