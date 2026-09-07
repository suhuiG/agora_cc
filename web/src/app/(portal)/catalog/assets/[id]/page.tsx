"use client";

import { useEffect, useRef, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import useSWR, { mutate } from "swr";
import Link from "next/link";
import {
  getAsset,
  listVersions,
  readSourceFile,
  recordView,
  recordDownload,
  setVersionVisibility,
  getInstallInstruction,
  detectOs,
  updateCuration,
  purgeAsset,
  SESSION_PRINCIPAL,
  type VersionInfo,
  type InstallInstruction,
  type OsId,
  type PurgeReport,
} from "@/lib/api";
import { ASSET_TYPE_META } from "@/lib/assetTypes";
import { DescriptorView } from "@/components/DescriptorView";
import { RedeployAgentModal } from "@/components/catalog/RedeployAgentModal";
import { RedeployMcpModal } from "@/components/catalog/RedeployMcpModal";
import { TrustBadge } from "@/components/catalog/TrustBadge";
import { TrustSummaryDetails } from "@/components/catalog/TrustSummaryDetails";
import { recommendCurations } from "@/lib/recommend";
import { registryStatusLabel, registryStatusBadgeClass } from "@/lib/governance";
import { getAuthSession, type AuthSession } from "@/lib/auth-client";
import { principalPresentation } from "@/lib/principal";
import { Button } from "@/components/ui/button";
import { Input, Select } from "@/components/ui/input";
import { Modal } from "@/components/ui/modal";
import { useToast } from "@/components/ui/toast";
import { ConversationManagerStatus } from "@/components/playground/ConversationManagerStatus";
import { conversationManagerFromDescriptor } from "@/lib/conversationManager";
import {
  PURGE_SUCCESS_TITLE,
  purgeOutcome,
  purgeSuccessDetail,
  type PurgeOutcome,
} from "@/lib/purgeOutcome";

// 소스 모드 자산은 descriptors 가 { "skill": { "sourcePrefix": "skill/{asset_id}/{version}/" } }
// 같은 모양이에요. 알려진 키(skill/mcp/agent) 안에서 sourcePrefix(string)를 찾아 돌려줘요.
// 없으면(인라인/레퍼런스 자산) null 을 돌려줘요.
const SOURCE_KEYS = ["skill", "mcp", "agent"] as const;

function extractSourcePrefix(descriptors: Record<string, unknown>): string | null {
  for (const key of SOURCE_KEYS) {
    const node = descriptors[key];
    if (node && typeof node === "object") {
      const prefix = (node as Record<string, unknown>).sourcePrefix;
      if (typeof prefix === "string" && prefix.length > 0) {
        return prefix;
      }
    }
  }
  return null;
}

// hosted MCP면 endpoint를 돌려줘요(설치 버튼 노출 판단용). 아니면 null.
function mcpEndpointOf(descriptors: Record<string, unknown>): string | null {
  const mcp = descriptors?.mcp as Record<string, unknown> | undefined;
  const ep = mcp?.endpoint;
  return typeof ep === "string" && ep.length > 0 ? ep : null;
}

function mcpToolsOf(descriptors: Record<string, unknown>): unknown[] {
  const mcp = descriptors?.mcp as Record<string, unknown> | undefined;
  const inline = (mcp?.tools as Record<string, unknown> | undefined)?.inlineContent;
  if (typeof inline !== "string") return [];

  try {
    const parsed = JSON.parse(inline) as unknown;
    if (!parsed || typeof parsed !== "object") return [];
    const tools = (parsed as Record<string, unknown>).tools;
    return Array.isArray(tools) ? tools : [];
  } catch {
    return [];
  }
}

function assetTypeSummary(
  descriptorType: string,
  descriptors: Record<string, unknown>,
): string | null {
  if (descriptorType === "MCP") {
    return `제공 Tool ${mcpToolsOf(descriptors).length}개`;
  }
  if (descriptorType === "Agent") {
    const agent = descriptors.agent as Record<string, unknown> | undefined;
    const model =
      typeof agent?.model === "string" && agent.model.trim()
        ? agent.model.trim()
        : null;
    // 서버는 `agoraDependencies`를 `{version, mcpAssets: [...]}` 객체로 저장해요
    // (deploy/service.py). 배열로 가정하면 의존 Tool이 항상 0으로 보여요.
    // 형태가 바뀔 여지가 있어 배열 형태도 함께 받아 방어해요.
    const deps = agent?.agoraDependencies as
      | { mcpAssets?: unknown[] }
      | unknown[]
      | undefined;
    const dependencyCount = Array.isArray(deps)
      ? deps.length
      : Array.isArray(deps?.mcpAssets)
        ? deps.mcpAssets.length
        : 0;
    return [model, `의존 Tool ${dependencyCount}개`].filter(Boolean).join(" · ");
  }
  return null;
}

// agent 자산의 A2A 호출 endpoint를 돌려줘요(복사 UI 노출 판단용). 아니면 null.
// DescriptorView.parseAgentCard 와 같은 규칙: descriptors.agent.endpoint 우선,
// 없으면 seed 데이터의 agentCard.endpoint 로 폴백해요.
function agentEndpointOf(descriptors: Record<string, unknown>): string | null {
  const agent = descriptors?.agent as Record<string, unknown> | undefined;
  const card = agent?.agentCard as Record<string, unknown> | undefined;
  const ep = agent?.endpoint ?? card?.endpoint;
  return typeof ep === "string" && ep.length > 0 ? ep : null;
}

// prefix = "skill/{owner}/{name}/{version}/" 예요. asset_id 는 type 과 version 사이의
// 모든 세그먼트({owner}/{name})를 합친 거예요(네임스페이스 식별자, §11.1).
function assetIdFromPrefix(prefix: string): string | null {
  const parts = prefix.split("/").filter(Boolean); // [type, owner, name, version]
  if (parts.length < 4) return null;
  const assetId = parts.slice(1, -1).join("/"); // owner/name
  return assetId.length > 0 ? assetId : null;
}

// 상태 배지 색상: PUBLISHED 는 초록, 그 외(DEPRECATED 등)는 회색이에요.
function statusBadgeClass(status: string): string {
  return status.toUpperCase() === "PUBLISHED"
    ? "bg-green-100 text-green-700"
    : "bg-slate-100 text-slate-500";
}

export default function AssetDetailPage() {
  const params = useParams();
  const router = useRouter();
  const id = params.id as string;
  const [install, setInstall] = useState<InstallInstruction | null>(null);
  const [installOs, setInstallOs] = useState<OsId>(detectOs());
  const [copied, setCopied] = useState(false);
  const [endpointCopied, setEndpointCopied] = useState(false);
  const [agentEndpointCopied, setAgentEndpointCopied] = useState(false);
  const [recordIdCopied, setRecordIdCopied] = useState(false);
  const [installError, setInstallError] = useState("");
  const [editing, setEditing] = useState(false);
  const [editForm, setEditForm] = useState({ description: "", tags: "", category: "", changelog: "" });
  const [editError, setEditError] = useState("");
  const [busy, setBusy] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [confirmName, setConfirmName] = useState("");
  const [purgeReport, setPurgeReport] = useState<
    { report: PurgeReport; outcome: PurgeOutcome } | null
  >(null);
  const [redeploying, setRedeploying] = useState(false);
  const [redeployStarted, setRedeployStarted] = useState(false);
  const { success, error: toastError } = useToast();
  // 삭제 확인 모달의 초기 포커스 대상 — 이름을 바로 입력할 수 있게요.
  const purgeInputRef = useRef<HTMLInputElement>(null);

  // 상세 데이터 — SWR 캐시(재방문 즉시 렌더, 경합·중복은 SWR이 처리).
  const assetKey = id ? (["/api/assets", id] as const) : null;
  const { data: asset, error: assetError } = useSWR(assetKey, () => getAsset(id));
  const { data: authSession } = useSWR<AuthSession>(
    "/api/auth/session",
    () => getAuthSession(),
  );

  // 소스 모드면 descriptors에서 asset_id를 도출해 버전 이력을 받아와요(파생값).
  const sourcePrefix = asset ? extractSourcePrefix(asset.descriptors) : null;
  const assetIdForVersions = sourcePrefix ? assetIdFromPrefix(sourcePrefix) : null;
  const { data: versions = [] } = useSWR<VersionInfo[]>(
    assetIdForVersions ? (["/api/versions", assetIdForVersions] as const) : null,
    () => listVersions(assetIdForVersions as string),
  );

  // 소스 모드 skill이면 SKILL.md 본문을 S3에서 읽어와 스킬 정의 섹션에 렌더해요.
  // (descriptors엔 sourcePrefix만 있고 본문은 S3에만 있어서 별도로 불러와요.)
  const isSkillSource = asset?.descriptor_type === "Agent Skills" && !!assetIdForVersions;
  const { data: skillFile } = useSWR(
    isSkillSource ? (["/api/skillmd", assetIdForVersions, asset!.version] as const) : null,
    () => readSourceFile(assetIdForVersions as string, asset!.version, "SKILL.md"),
  );

  // 조회수는 상세 진입 시 1회만 기록해요(POST /view). id별 가드로 StrictMode 중복 방지.
  const viewedRef = useRef<string | null>(null);
  useEffect(() => {
    if (!id || viewedRef.current === id) return;
    viewedRef.current = id;
    void recordView(id); // 실패는 무시 — 화면 영향 없음
  }, [id]);

  async function openInstall(version: string) {
    if (asset?.status !== "APPROVED") return;
    setInstallError("");
    try {
      const ins = await getInstallInstruction(id, { tool: "claude", os: installOs, version });
      setInstall(ins);
      setCopied(false);
    } catch {
      setInstallError("설치 명령을 불러오지 못했어요.");
      toastError("설치 명령을 불러오지 못했어요.", "잠시 후 다시 시도해 주세요.");
    }
  }

  // 클립보드 API 는 권한 거부·비보안 컨텍스트에서 reject 돼요. try 없이 두면 핸들러가
  // 그대로 실패해 성공·실패 알림이 둘 다 안 떠요.
  // 성공했으면 true. 호출부가 이 값으로 후속 동작(다운로드 기록 등)을 건너뛸 수 있어요.
  async function copyToClipboard(
    text: string,
    onCopied: () => void,
    label: string,
  ): Promise<boolean> {
    try {
      await navigator.clipboard.writeText(text);
      onCopied();
      success(`${label}을 복사했어요.`);
      return true;
    } catch {
      toastError(
        `${label}을 복사하지 못했어요.`,
        "브라우저가 클립보드 접근을 막았어요. 직접 선택해 복사해 주세요.",
      );
      return false;
    }
  }

  async function copyInstall() {
    if (install?.command) {
      await copyToClipboard(install.command, () => setCopied(true), "설치 명령");
    }
  }

  // hosted MCP의 호출 endpoint를 클립보드로 복사해요. 설치 모달의 copied와 별개 state로 피드백을 줘요.
  async function copyEndpoint() {
    if (mcpEndpoint) {
      const copiedOk = await copyToClipboard(
        mcpEndpoint, () => setEndpointCopied(true), "MCP endpoint",
      );
      // 복사가 실패했으면 다운로드로 세지 않아요 — 사용자는 endpoint 를 못 받았어요.
      if (!copiedOk) return;
      // 다운로드 기록 + 화면 갱신(실패는 무시)
      recordDownload(id).then(() => { void mutate(assetKey); }).catch(() => {});
    }
  }

  // agent 자산의 A2A 호출 endpoint를 클립보드로 복사해요. MCP endpointCopied와 별개 state예요.
  async function copyAgentEndpoint() {
    if (agentEndpoint) {
      const copiedOk = await copyToClipboard(
        agentEndpoint, () => setAgentEndpointCopied(true), "A2A endpoint",
      );
      if (!copiedOk) return;
      // 다운로드 기록 + 화면 갱신(실패는 무시)
      recordDownload(id).then(() => { void mutate(assetKey); }).catch(() => {});
    }
  }

  async function toggleVisible(v: VersionInfo) {
    if (!assetIdForVersions) return;
    const next = !v.search_visible;
    const key = ["/api/versions", assetIdForVersions] as const;
    const flip = (list: VersionInfo[], to: boolean) =>
      list.map((x) => (x.version === v.version ? { ...x, search_visible: to } : x));
    // 낙관적 업데이트 (SWR 캐시 직접 변경, 재검증 보류)
    await mutate(key, flip(versions, next), { revalidate: false });
    try {
      await setVersionVisibility(assetIdForVersions, v.version, next);
    } catch (e) {
      // 실패 시 롤백. 조용히 되돌리면 사용자가 "왜 안 바뀌었지?" 하게 되니 알려줘요.
      await mutate(key, flip(versions, !next), { revalidate: false });
      toastError(
        "검색 노출 설정을 바꾸지 못했어요.",
        e instanceof Error ? e.message : `v${v.version} 설정이 원래대로 돌아갔어요.`,
      );
    }
  }

  function startEdit() {
    if (!asset) return;
    setEditForm({
      description: asset.description ?? "",
      tags: (asset.tags ?? []).join(", "),
      category: asset.category ?? "",
      changelog: "", // changelog는 상세 detail엔 없을 수 있어 빈값에서 시작(입력 시에만 갱신)
    });
    setEditError("");
    setEditing(true);
  }

  async function saveEdit() {
    if (!asset) return;
    setBusy(true);
    setEditError("");
    try {
      const fields: Parameters<typeof updateCuration>[1] = {
        description: editForm.description,
        tags: editForm.tags.split(",").map((t) => t.trim()).filter(Boolean),
        category: editForm.category,
      };
      if (editForm.changelog.trim()) fields.changelog = editForm.changelog.trim();
      const updated = await updateCuration(asset.record_id, fields, SESSION_PRINCIPAL);
      // 갱신 결과를 SWR 캐시에 반영(재검증 없이 즉시).
      await mutate(assetKey, updated, { revalidate: false });
      setEditing(false);
      success("수정했어요.", `${asset.name} 의 설명·태그·카테고리를 갱신했어요.`);
    } catch (e) {
      const message = e instanceof Error ? e.message : "수정에 실패했어요.";
      setEditError(message);
      toastError("수정하지 못했어요.", message);
    } finally {
      setBusy(false);
    }
  }

  // 추천 채우기: 이름·설명·(MCP면)tool명 키워드로 카테고리·태그를 제안해 edit 폼에 채워요.
  function applyRecommendations() {
    if (!asset) return;
    const toolNames = mcpToolsOf(asset.descriptors)
      .map((tool) =>
        tool && typeof tool === "object"
          ? (tool as Record<string, unknown>).name
          : null,
      )
      .filter((name): name is string => typeof name === "string");
    const rec = recommendCurations(asset.name, editForm.description || asset.description || "", toolNames);
    setEditForm((p) => ({
      ...p,
      category: p.category || rec.category,
      tags: p.tags.trim() ? p.tags : rec.tags.join(", "),
    }));
  }

  async function doPurge() {
    if (!asset) return;
    setBusy(true);
    try {
      const { report, message } = await purgeAsset(
        asset.record_id,
        SESSION_PRINCIPAL,
      );
      // ⚠️ 판정 기준이 「실패가 있나」가 아니라 **「자산 레코드가 지워졌나」** 예요.
      // 옛 코드는 `report.failed.length > 0` 만 봐서 두 가지를 틀렸어요 — 레코드가 보존된
      // 경우에도 모달 제목이 「삭제 완료」였고(서버는 「보존했어요」라고 말하는데 그 `message`
      // 를 버렸어요), 실패가 없는데 레코드만 남은 경로에서는 모달조차 안 뜨고 목록으로
      // 보냈어요. 판정은 `purgeOutcome` 이 소유해요(`.tsx` 는 테스트 러너가 못 돌려요).
      const outcome = purgeOutcome(report, message);
      // 리포트 모달을 띄우기 전에 확인 모달을 닫아, 두 오버레이가 겹치지 않게 해요.
      setConfirmDelete(false);
      if (outcome.showReport) {
        setPurgeReport({ report, outcome });
      } else {
        success(PURGE_SUCCESS_TITLE, purgeSuccessDetail(asset.name));
        router.push("/catalog/browse");
      }
    } catch (e) {
      const message = e instanceof Error ? e.message : "완전 삭제에 실패했어요.";
      // 예전엔 editError(수정 폼 전용 슬롯)에 담았는데, 삭제 시점엔 그 폼이 닫혀 있어서
      // 실패가 화면에 아예 안 보였어요. 토스트로만 알려요.
      toastError("삭제하지 못했어요.", message);
      setBusy(false);
      setConfirmDelete(false);
    }
  }

  if (assetError)
    return (
      <div className="text-center py-16">
        <p className="text-slate-500">자산을 찾을 수 없어요.</p>
        <Link href="/catalog/browse" className="text-blue-600 underline mt-4 inline-block">
          카탈로그로 돌아가기
        </Link>
      </div>
    );

  if (!asset)
    return <div className="text-center py-16 text-slate-500">불러오는 중...</div>;

  const typeInfo = ASSET_TYPE_META[asset.descriptor_type];
  const isSourceMode = sourcePrefix !== null;
  const mcpEndpoint = mcpEndpointOf(asset.descriptors);
  const isHostedMcp = mcpEndpoint !== null;
  const typeSummary = assetTypeSummary(asset.descriptor_type, asset.descriptors);
  // agent 자산이면 A2A 호출 endpoint를 뽑아요(없으면 null → 복사 카드 미노출).
  const agentEndpoint = asset.descriptor_type === "Agent" ? agentEndpointOf(asset.descriptors) : null;
  const conversationManager = asset.descriptor_type === "Agent"
    ? conversationManagerFromDescriptor(asset.descriptors)
    : null;
  // 설치 가능 여부 — Claude Code에 설치 레시피가 있는 타입만 설치 버튼을 보여줘요.
  // 소스 모드에선 skill(Agent Skills)만 설치 가능하고, 소스 Agent는 설치 레시피가 없어
  // 백엔드가 422를 주니까(정상 계약) 버튼을 감춰요. hosted MCP는 별도 설치 카드에서 노출해요.
  // isInstallableSkill 은 버전 이력 테이블의 설치 컬럼·버튼을 게이팅하는 값이에요.
  const isInstallableSkill = isSourceMode && asset.descriptor_type === "Agent Skills";
  const installationApproved = asset.status === "APPROVED";
  const installDisabledReason = installationApproved
    ? undefined
    : "심사 승인 후 설치할 수 있어요";
  const ownerPrincipal = principalPresentation(
    asset.owner_email || asset.owner_user,
  );
  const escalationPrincipal = principalPresentation(asset.escalation_contact);
  const isOwner =
    authSession?.authenticated === true &&
    asset.owner_user === authSession.principal_id;
  // 재배포 가능 여부 — 배포형 agent만이에요. 도메인 연결로 등록한 agent는 우리가 호스팅하는
  // 게 아니라 갱신할 runtime이 없어요. Runtime ARN 은 응답에서 숨기므로(IA-89 ①), 배포형
  // 여부는 서버가 내려주는 runtime_deployed boolean으로 판단해요(MCP 의 source_managed 와 동형).
  const isRedeployable =
    isOwner &&
    asset.descriptor_type === "Agent" &&
    asset.runtime_deployed === true;
  // 배포형 MCP 재배포 가능 여부 — 소스가 있는 MCP(Lambda+Gateway로 호스팅)만이에요.
  // MCP는 sourcePrefix 좌표를 응답에서 숨기므로(CA-04) isSourceMode가 항상 false예요 —
  // 배포형 여부는 서버가 내려주는 source_managed boolean으로 판단해요(CA-16·ADR-0021).
  // 연결형 MCP(외부 endpoint만, 소스 없음)는 갱신할 우리 코드가 없어 제외돼요.
  const isMcpRedeployable =
    isOwner && asset.descriptor_type === "MCP" && asset.source_managed === true;
  // listVersions 는 오름차순(semver)이라 화면에는 최신순으로 뒤집어 보여줘요.
  const orderedVersions = [...versions].reverse();

  return (
    // 본문 폭 캡 — 이 화면만 캡이 없어서 넓은 모니터에서 문단 한 줄이 2200px 넘게
    // 늘어났어요(2026-08-29, 2560px 에서 실측). 포털 layout 의 `main` 은 `max-width`
    // 가 없고, 이 저장소는 화면마다 캡을 거는 관례예요(`/governance`·`/playground`·
    // Initializr 가 `max-w-6xl`·`max-w-[1400px]`). 상세는 표가 넓어서 6xl 보다 여유를 둬요.
    <div className="mx-auto max-w-[1400px]">
      <Link href="/catalog/browse" className="text-sm text-blue-600 hover:underline mb-4 inline-block">
        ← 카탈로그
      </Link>

      <div className="bg-white border border-slate-200 rounded-xl p-6 mb-6">
        <div className="flex items-center gap-3 mb-4">
          <span
            className={`text-sm font-medium px-3 py-1 rounded-full ${
              typeInfo?.pill || "bg-gray-100"
            }`}
          >
            {typeInfo?.label || asset.descriptor_type}
          </span>
          <span className="text-sm text-slate-500">v{asset.version}</span>
          <span
            className={`text-sm font-medium px-3 py-1 rounded-full ml-auto ${registryStatusBadgeClass(
              asset.status,
            )}`}
          >
            {registryStatusLabel(asset.status)}
          </span>
        </div>

        <div className="flex items-center justify-between gap-3 mb-2">
          <h1 className="text-2xl font-bold">{asset.name}</h1>
          {isOwner && !editing && (
            <div className="flex gap-2 shrink-0">
              {isRedeployable && (
                <button
                  type="button"
                  onClick={() => setRedeploying(true)}
                  className="text-sm px-3 py-1.5 border border-blue-200 text-blue-700 rounded-lg hover:bg-blue-50"
                >
                  재배포
                </button>
              )}
              {isMcpRedeployable && (
                <button
                  type="button"
                  onClick={() => setRedeploying(true)}
                  className="text-sm px-3 py-1.5 border border-blue-200 text-blue-700 rounded-lg hover:bg-blue-50"
                >
                  재배포
                </button>
              )}
              <button
                type="button"
                onClick={startEdit}
                className="text-sm px-3 py-1.5 border border-slate-300 rounded-lg hover:bg-slate-50"
              >
                수정
              </button>
              <button
                type="button"
                onClick={() => { setConfirmName(""); setConfirmDelete(true); }}
                disabled={busy}
                className="text-sm px-3 py-1.5 border border-red-200 text-red-600 rounded-lg hover:bg-red-50 disabled:opacity-40"
              >
                완전 삭제
              </button>
            </div>
          )}
        </div>
        <p className="text-slate-600 mb-4">
          {asset.description?.trim() ? (
            asset.description
          ) : (
            <span className="text-slate-300">설명이 없어요</span>
          )}
        </p>

        <div className="mb-4">
          <span className="mb-1 block text-xs text-slate-500">Record ID</span>
          <div className="flex items-center gap-2">
            <input
              type="text"
              readOnly
              value={asset.record_id}
              onFocus={(event) => event.currentTarget.select()}
              className="min-w-0 flex-1 rounded-lg border border-slate-300 bg-slate-50 px-2 py-1.5 font-mono text-xs text-slate-700 focus:outline-none focus:ring-2 focus:ring-blue-500"
            />
            <button
              type="button"
              onClick={() =>
                copyToClipboard(
                  asset.record_id,
                  () => setRecordIdCopied(true),
                  "Record ID",
                )
              }
              className="shrink-0 rounded-lg border border-slate-300 px-3 py-1.5 text-sm hover:bg-slate-50"
            >
              {recordIdCopied ? "복사됨" : "복사"}
            </button>
          </div>
        </div>

        <TrustBadge trust={asset.trust} />

        {editing && (
          <div className="border border-slate-200 rounded-lg p-4 mb-4 space-y-3 bg-slate-50">
            <p className="text-sm font-medium text-slate-700">큐레이션 수정 (이름·버전·소스는 못 바꿔요)</p>
            <div>
              <label className="block text-xs text-slate-500 mb-1">설명</label>
              <textarea
                value={editForm.description}
                onChange={(e) => setEditForm((p) => ({ ...p, description: e.target.value }))}
                rows={2}
                className="w-full p-2 border border-slate-300 rounded text-sm bg-white"
              />
            </div>
            <div className="flex items-center justify-between">
              <span className="text-xs text-slate-500">카테고리·태그</span>
              <button
                type="button"
                onClick={applyRecommendations}
                className="text-xs px-2 py-1 border border-blue-200 text-blue-600 rounded hover:bg-blue-50"
              >
                ✨ 추천 채우기
              </button>
            </div>
            <div className="grid grid-cols-2 gap-3">
              <div>
                <label className="block text-xs text-slate-500 mb-1">태그 (쉼표)</label>
                <input
                  value={editForm.tags}
                  onChange={(e) => setEditForm((p) => ({ ...p, tags: e.target.value }))}
                  className="w-full p-2 border border-slate-300 rounded text-sm bg-white"
                />
              </div>
              <div>
                <label className="block text-xs text-slate-500 mb-1">카테고리</label>
                <input
                  value={editForm.category}
                  onChange={(e) => setEditForm((p) => ({ ...p, category: e.target.value }))}
                  className="w-full p-2 border border-slate-300 rounded text-sm bg-white"
                />
              </div>
            </div>
            <div>
              <label className="block text-xs text-slate-500 mb-1">변경사항(이 버전, 선택)</label>
              <textarea
                value={editForm.changelog}
                onChange={(e) => setEditForm((p) => ({ ...p, changelog: e.target.value }))}
                rows={2}
                placeholder="비우면 기존 changelog 유지"
                className="w-full p-2 border border-slate-300 rounded text-sm bg-white"
              />
            </div>
            {editError && <p className="text-sm text-red-600">{editError}</p>}
            <div className="flex gap-2">
              <button
                type="button"
                onClick={saveEdit}
                disabled={busy}
                className="text-sm px-4 py-1.5 bg-slate-900 text-white rounded-lg hover:bg-slate-700 disabled:opacity-40"
              >
                {busy ? "저장 중..." : "저장"}
              </button>
              <button
                type="button"
                onClick={() => setEditing(false)}
                className="text-sm px-4 py-1.5 border border-slate-300 rounded-lg hover:bg-slate-50"
              >
                취소
              </button>
            </div>
          </div>
        )}

        <div className="flex flex-wrap gap-2 mb-4">
          {asset.tags.map((tag) => (
            <span
              key={tag}
              className="text-sm bg-slate-100 text-slate-700 px-3 py-1 rounded-full"
            >
              {tag}
            </span>
          ))}
        </div>

        <div className="grid grid-cols-2 md:grid-cols-6 gap-4 text-sm">
          <div>
            <span className="text-slate-500">카테고리</span>
            <p className="font-medium">{asset.category}</p>
          </div>
          <div>
            <span className="text-slate-500">소유 팀</span>
            <p className="font-medium">{asset.owner_team}</p>
          </div>
          <div>
            <span className="text-slate-500">담당자</span>
            <p className="font-medium" title={ownerPrincipal.title}>
              {ownerPrincipal.label}
            </p>
          </div>
          <div>
            <span className="text-slate-500">담당자(2차)</span>
            <p className="font-medium" title={escalationPrincipal.title}>
              {escalationPrincipal.label}
            </p>
          </div>
          <div>
            <span className="text-slate-500">조회수</span>
            <p className="font-medium">{asset.views}</p>
          </div>
          <div>
            {/* agent는 내려받는 게 아니라 endpoint로 호출해 쓰는 자산이라 '호출 횟수'로
                불러요. 집계 값(downloads)은 같고 라벨만 타입에 맞춰 바꿔요. */}
            <span className="text-slate-500">
              {asset.descriptor_type === "Agent" ? "호출 횟수" : "다운로드"}
            </span>
            <p className="font-medium">{asset.downloads}</p>
          </div>
        </div>
      </div>

      {asset.trust && (
        <div className="bg-white border border-slate-200 rounded-xl p-6 mb-6">
          <div className="pb-2 mb-4 border-b border-slate-300">
            <span className="text-base font-semibold text-slate-800">신뢰 요약</span>
          </div>
          <TrustSummaryDetails trust={asset.trust} />
          {typeSummary && (
            <div className="mt-4 border-t border-slate-200 pt-4">
              <p className="mb-1 text-xs text-slate-500">구성</p>
              <p className="text-sm font-medium text-slate-800">{typeSummary}</p>
            </div>
          )}
        </div>
      )}

      {/* hosted MCP 설치 — 스펙/tool 정의 카드 위에 노출해요(설치 먼저 → 상세 스펙). */}
      {isHostedMcp && (
        <div className="bg-white border border-slate-200 rounded-xl p-6 mb-6">
          <div className="pb-2 mb-3 border-b border-slate-300">
            <span className="text-base font-semibold text-slate-800">설치</span>
          </div>
          <p className="text-sm text-slate-600 mb-3">
            이 MCP를 내 tool 설정에 등록해요.
          </p>
          {mcpEndpoint && (
            <div className="mb-3">
              <span className="block text-xs text-slate-500 mb-1">호출 endpoint</span>
              <div className="flex items-center gap-2">
                <input
                  type="text"
                  readOnly
                  value={mcpEndpoint}
                  onFocus={(e) => e.currentTarget.select()}
                  className="flex-1 min-w-0 px-2 py-1.5 border border-slate-300 rounded-lg bg-slate-50 font-mono text-xs text-slate-700 focus:outline-none focus:ring-2 focus:ring-blue-500"
                />
                <button
                  type="button"
                  onClick={copyEndpoint}
                  className="shrink-0 px-3 py-1.5 border border-slate-300 text-sm rounded-lg hover:bg-slate-50"
                >
                  {endpointCopied ? "복사됨 ✓" : "복사"}
                </button>
              </div>
            </div>
          )}
          <span className="inline-block" title={installDisabledReason}>
            <button
              type="button"
              onClick={() => openInstall(asset!.version)}
              disabled={!installationApproved}
              className="px-4 py-2 bg-slate-900 text-white text-sm rounded-lg hover:bg-slate-700 disabled:cursor-not-allowed disabled:opacity-40"
            >
              설치 명령 보기
            </button>
          </span>
        </div>
      )}

      {/* agent 호출 endpoint — 복사 카드. 스펙 카드 위에 노출해요.
          endpoint 없으면(가드) 렌더하지 않아요. */}
      {agentEndpoint && (
        <div className="bg-white border border-slate-200 rounded-xl p-6 mb-6">
          <div className="pb-2 mb-3 border-b border-slate-300">
            <span className="text-base font-semibold text-slate-800">호출 정보</span>
          </div>
          {agentEndpoint && (
            <div className="mb-4">
              <span className="block text-xs text-slate-500 mb-1">호출 endpoint (A2A)</span>
              <div className="flex items-center gap-2">
                <input
                  type="text"
                  readOnly
                  value={agentEndpoint}
                  onFocus={(e) => e.currentTarget.select()}
                  className="flex-1 min-w-0 px-2 py-1.5 border border-slate-300 rounded-lg bg-slate-50 font-mono text-xs text-slate-700 focus:outline-none focus:ring-2 focus:ring-blue-500"
                />
                <button
                  type="button"
                  onClick={copyAgentEndpoint}
                  className="shrink-0 px-3 py-1.5 border border-slate-300 text-sm rounded-lg hover:bg-slate-50"
                >
                  {agentEndpointCopied ? "복사됨 ✓" : "복사"}
                </button>
              </div>
            </div>
          )}
        </div>
      )}

      {asset.descriptor_type === "Agent" && (
        <div className="mb-6 border-y border-slate-200 py-4">
          <span className="block text-xs text-slate-500">
            실제 생성된 context 전략
          </span>
          <div className="mt-1">
            <ConversationManagerStatus manager={conversationManager} />
          </div>
        </div>
      )}

      {/* 스펙 / tool 정의 — 타입별로 구조화해 보여줘요. */}
      <div className="bg-white border border-slate-200 rounded-xl p-6 mb-6">
        <div className="pb-2 mb-4 border-b border-slate-300">
          <span className="text-base font-semibold text-slate-800">
            {asset.descriptor_type === "MCP"
              ? "제공하는 도구 (Tools)"
              : asset.descriptor_type === "Agent"
                ? "에이전트 스펙"
                : asset.descriptor_type === "Agent Skills"
                  ? "스킬 정의 (SKILL.md)"
                  : asset.descriptor_type === "Model"
                    ? "모델 정보"
                    : asset.descriptor_type === "App"
                      ? "앱 정보"
                      : "정의 (Descriptors)"}
          </span>
          {typeInfo?.blurb && (
            <p className="mt-1 text-sm text-slate-500">{typeInfo.blurb}</p>
          )}
        </div>
        <DescriptorView
          descriptorType={asset.descriptor_type}
          descriptors={asset.descriptors}
          sourcePrefix={sourcePrefix}
          skillMarkdown={skillFile?.content ?? null}
        />
      </div>

      {/* 버전 이력 + 감사(등록자) — 소스 모드 자산에만 보여줘요. */}
      {isSourceMode && (
        <div className="bg-white border border-slate-200 rounded-xl p-6 mb-6">
          <div className="pb-2 mb-4 border-b border-slate-300">
            <span className="text-base font-semibold text-slate-800">버전 이력</span>
            {orderedVersions.length > 0 && (
              <span className="text-sm text-slate-400 ml-2">{orderedVersions.length}개</span>
            )}
          </div>

          {orderedVersions.length > 0 ? (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="text-left text-xs text-slate-500 border-b border-slate-200">
                    <th className="py-2 pr-4 font-medium">버전</th>
                    <th className="py-2 pr-4 font-medium">상태</th>
                    <th className="py-2 pr-4 font-medium">등록자</th>
                    <th className="py-2 pr-4 font-medium">등록 시각</th>
                    <th className="py-2 pr-4 font-medium">파일</th>
                    <th className="py-2 pr-4 font-medium">변경사항</th>
                    <th className="py-2 pr-4 font-medium">노출</th>
                    {/* 설치 컬럼은 설치 가능한 skill에만 노출해요(소스 Agent는 설치 레시피가 없어요). */}
                    {isInstallableSkill && <th className="py-2 font-medium">설치</th>}
                  </tr>
                </thead>
                <tbody>
                  {orderedVersions.map((v) => {
                    const publisher = principalPresentation(v.published_by);
                    return (
                      <tr
                        key={v.version}
                        className="border-b border-slate-100 last:border-0 align-top"
                      >
                        <td className="py-3 pr-4 font-medium text-slate-800 whitespace-nowrap">
                          v{v.version}
                        </td>
                        <td className="py-3 pr-4 whitespace-nowrap">
                          <span
                            className={`text-xs font-medium px-2 py-0.5 rounded-full ${statusBadgeClass(
                              v.status,
                            )}`}
                          >
                            {v.status}
                          </span>
                        </td>
                        <td className="py-3 pr-4">
                          <code
                            className="font-mono text-xs text-slate-700 break-all"
                            title={publisher.title}
                          >
                            {publisher.label}
                          </code>
                        </td>
                        <td className="py-3 pr-4 text-slate-500 whitespace-nowrap">
                          {v.published_at}
                        </td>
                        <td className="py-3 pr-4 text-slate-500 whitespace-nowrap">
                          {v.file_count}개 파일
                        </td>
                        <td className="py-3 pr-4 text-slate-600 max-w-xs">
                          {v.changelog || <span className="text-slate-300">—</span>}
                        </td>
                        <td className="py-3 pr-4 whitespace-nowrap">
                          <button
                            type="button"
                            onClick={() => toggleVisible(v)}
                            className={`text-xs px-2 py-1 rounded ${
                              v.search_visible
                                ? "bg-emerald-50 text-emerald-700"
                                : "bg-slate-100 text-slate-400"
                            }`}
                            title="메인/검색 노출 토글"
                          >
                            {v.search_visible ? "노출 중" : "숨김"}
                          </button>
                        </td>
                        {/* 설치 버튼은 설치 가능한 skill에만 렌더해요. 소스 Agent는 설치 레시피가 없어
                            버튼을 눌러도 백엔드가 422를 주니까 아예 감춰요(헤더 컬럼과 짝을 맞춰요). */}
                        {isInstallableSkill && (
                          <td className="py-3 whitespace-nowrap">
                            <span className="inline-block" title={installDisabledReason}>
                              <button
                                type="button"
                                onClick={() => openInstall(v.version)}
                                disabled={!installationApproved}
                                className="text-xs px-3 py-1 bg-slate-900 text-white rounded hover:bg-slate-700 disabled:cursor-not-allowed disabled:opacity-40"
                              >
                                설치
                              </button>
                            </span>
                          </td>
                        )}
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          ) : (
            <p className="text-sm text-slate-500">버전 정보를 불러오는 중이거나 없어요.</p>
          )}
        </div>
      )}

      {redeploying && asset && isRedeployable && (
        <RedeployAgentModal
          recordId={id}
          assetName={asset.name}
          currentVersion={asset.version}
          onClose={() => setRedeploying(false)}
          onStarted={() => setRedeployStarted(true)}
        />
      )}

      {redeploying && asset && isMcpRedeployable && (
        <RedeployMcpModal
          recordId={id}
          assetName={asset.name}
          currentVersion={asset.version}
          onClose={() => setRedeploying(false)}
          onStarted={() => setRedeployStarted(true)}
        />
      )}

      {/* 재배포는 수 분 걸려서 화면에서 대기하지 않아요 — 진행은 '나의 요청'에서 봐요. */}
      {redeployStarted && (
        <div className="mt-4 rounded-lg border border-blue-200 bg-blue-50 p-4 text-sm text-blue-800">
          재배포를 시작했어요. 빌드·스캔·배포가 진행되는 동안{" "}
          <Link href="/catalog/requests" className="font-medium underline">
            나의 요청
          </Link>
          에서 상태를 확인할 수 있어요. 완료되면 이 페이지의 버전이 올라가요.
        </div>
      )}

      <Modal
        open={confirmDelete && Boolean(asset)}
        size="sm"
        busy={busy}
        title="자산 완전 삭제"
        initialFocusRef={purgeInputRef}
        onClose={() => setConfirmDelete(false)}
        description={
          <>
            <span className="font-medium text-foreground">{asset.name}</span>의 모든 버전과
            관련 데이터를 영구 삭제해요.
            <span className="mt-1.5 block text-xs text-red-600">
              소스·버전 이력·거버넌스 기록·배포된 AWS 리소스까지 지워지고, 되돌릴 수 없어요.
            </span>
          </>
        }
        footer={
          <>
            <Button variant="outline" size="md" onClick={() => setConfirmDelete(false)} disabled={busy}>
              취소
            </Button>
            <Button
              variant="destructive"
              size="md"
              onClick={doPurge}
              disabled={busy || confirmName !== asset.name}
            >
              {busy ? "삭제 중…" : "완전 삭제"}
            </Button>
          </>
        }
      >
        <label htmlFor="purge-confirm-name" className="mb-1.5 block text-xs text-muted-foreground">
          확인을 위해 자산 이름{" "}
          <span className="font-mono text-foreground">{asset.name}</span>을(를) 입력하세요
        </label>
        <Input
          id="purge-confirm-name"
          ref={purgeInputRef}
          value={confirmName}
          onChange={(e) => setConfirmName(e.target.value)}
          placeholder={asset.name}
        />
      </Modal>

      <Modal
        open={Boolean(purgeReport)}
        size="md"
        title={purgeReport?.outcome.title ?? ""}
        description={purgeReport?.outcome.description ?? ""}
        onClose={() => {
          // 레코드가 남아 있으면 목록으로 보내지 않아요 — 자산이 아직 있는데 목록으로
          // 튕기면 「지워졌다」로 읽혀요. 그 자리에서 다시 삭제할 수 있게 남겨 둬요.
          const leave = purgeReport?.outcome.leaveAfterClose ?? true;
          setPurgeReport(null);
          setBusy(false);
          if (leave) router.push("/catalog/browse");
        }}
        footer={
          <Button
            size="md"
            onClick={() => {
              const leave = purgeReport?.outcome.leaveAfterClose ?? true;
              setPurgeReport(null);
              setBusy(false);
              if (leave) router.push("/catalog/browse");
            }}
          >
            확인
          </Button>
        }
      >
        {/* ⚠️ `failed` 만 그리면 안 돼요. 레코드가 보존되는 경로 중에는 실패 없이 «건너뜀» 만
            남는 것이 있어서(정리 클리너 미주입), 그때 본문이 통째로 비어 「왜 안 지워졌는지」를
            화면이 말하지 못했어요. 두 목록을 함께 그리고, 둘 다 비면 그 사실도 밝혀요. */}
        <ul className="max-h-40 space-y-1 overflow-auto rounded-lg bg-muted p-3 text-xs text-muted-foreground">
          {purgeReport?.report.failed.map((f, i) => (
            <li key={`failed-${i}`}>
              <span className="font-medium text-red-600">{f.store}</span>: {f.reason}
            </li>
          ))}
          {purgeReport?.report.skipped.map((s, i) => (
            <li key={`skipped-${i}`}>
              <span className="font-medium text-amber-700">건너뜀</span>: {s}
            </li>
          ))}
          {purgeReport
            && purgeReport.report.failed.length === 0
            && purgeReport.report.skipped.length === 0 && (
            <li>
              서버가 실패나 건너뜀 항목을 남기지 않았어요 — 사유를 확인하지 못했어요.
            </li>
          )}
        </ul>
      </Modal>

      <Modal
        open={Boolean(install)}
        size="lg"
        title="내 환경에 설치"
        description={install?.note}
        onClose={() => setInstall(null)}
        footer={
          <>
            {install?.command && (
              <Button size="md" onClick={copyInstall}>
                {copied ? "복사됨 ✓" : "복사"}
              </Button>
            )}
            <Button variant="outline" size="md" onClick={() => setInstall(null)}>
              닫기
            </Button>
          </>
        }
      >
        <div className="mb-3 flex items-center gap-2 text-sm">
          <label htmlFor="install-os" className="text-muted-foreground">
            OS
          </label>
          <Select
            id="install-os"
            value={installOs}
            onChange={async (e) => {
              const os = e.target.value as OsId;
              setInstallOs(os);
              try {
                const ins = await getInstallInstruction(id, { tool: "claude", os });
                setInstall(ins);
              } catch {
                setInstallError("설치 명령을 불러오지 못했어요.");
              }
            }}
            className="h-8"
          >
            <option value="macos">macOS</option>
            <option value="linux">Linux</option>
            <option value="windows">Windows</option>
          </Select>
        </div>
        {installError && (
          <div role="alert" className="mb-3 rounded-lg bg-red-50 p-2 text-sm text-red-600">
            {installError}
          </div>
        )}
        {install?.command ? (
          <pre className="overflow-x-auto whitespace-pre-wrap break-all rounded-lg bg-slate-900 p-3 text-xs text-slate-100">
            {install.command}
          </pre>
        ) : (
          <div className="rounded-lg bg-amber-50 p-3 text-sm text-amber-700">
            배포 대기 중이에요.
          </div>
        )}
      </Modal>
    </div>
  );
}
