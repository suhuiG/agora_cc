"use client";

import { useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import useSWR from "swr";
import {
  ApiError,
  GOV_TIER_VALUES,
  approveGovAsset,
  getCognitoUser,
  getGovGates,
  getGovMe,
  patchGovTargetTier,
  rejectGovAsset,
  rescanGate,
  runOverlapReview,
  type GatesResponse,
  type GovMe,
  type GovTier,
  type OverlapReview,
} from "@/lib/api";
import { ADMIN_QUEUE } from "@/lib/adminNav";
import { areaLabel, enforcementLabel, tierLabel } from "@/lib/governance";
import { registryStatusBadgeClass, registryStatusLabel } from "@/lib/governance";
import { Icon } from "@/components/ui/icon";
import { Card } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Select } from "@/components/ui/input";
import { cn } from "@/lib/ui";
import { overlapPresentation, TONE_CLASSES, type TrustTone } from "@/lib/trust";
import { ApprovalBlockNotice } from "@/components/catalog/ApprovalBlockNotice";
import { GateStatusDot, RiskBadge } from "./gateBits";
import { ScanRunButton } from "./ScanRunButton";
import { ThreatReportButton } from "./ThreatReportButton";
import { GateLogModal } from "./GateLogModal";
import { ResponsibilityEditor } from "./ResponsibilityEditor";
import { useToast } from "@/components/ui/toast";
import {
  isEmailOwnerId,
  ownerPresentation,
  scanApplicabilityPresentation,
  scanRunAvailability,
} from "@/lib/assetDetailPresentation";

// 자산 상세 판정 (§3-M2) — 큐에서 자산명 클릭 시 진입.
export function ReviewClient({ recordId }: { recordId: string }) {
  const { data: me } = useSWR<GovMe>("gov/me", getGovMe);
  const { data: gates, error, isLoading, mutate } = useSWR<GatesResponse>(
    ["gov/gates", recordId], () => getGovGates(recordId),
    // 서버가 running이면 짧게 폴링해 running→done 전이를 자동 반영해요(큐에서 시작한 스캔 포함).
    { refreshInterval: (d) => (d?.scan_status === "running" ? 3000 : 0) },
  );
  // 재스캔 진행 중 = 로컬 클릭 직후(낙관적) 또는 서버가 이미 running(다른 화면에서 시작).
  const [localScanning, setLocalScanning] = useState(false);
  const serverScanning = gates?.scan_status === "running";
  // 부분 재스캔(rescan_area 있음)은 "전체 스캔중"이 아니에요 — 그 도구 행만 진행중으로
  // 보여야 해요. scanning을 전체 플래그로 쓰면 도구 하나만 재시도해도 전 게이트가
  // 스캔중으로 덮여요(2026-07-28 제품 오너 확인).
  const rescanArea = gates?.rescan_area ?? "";
  const scanning = localScanning || (serverScanning && !rescanArea);
  const canReview = me?.can.review ?? false;
  const canManageResponsibility = me?.roles.includes("admin") ?? false;
  const canManageThreatReportTier = me?.can.tier_write ?? false;
  const scanAvailability = gates
    ? scanRunAvailability(gates.asset.scan_applicability)
    : null;

  return (
    <div>
      <Link
        href={ADMIN_QUEUE}
        className="mb-4 inline-flex items-center gap-1.5 text-[13px] text-muted-foreground hover:text-foreground"
      >
        <Icon name="back" size={14} />
        승인 큐로
      </Link>

      <div className="mb-5 flex flex-wrap items-center justify-between gap-3">
        {/* 제목엔 자산명을 노출해요 — record_id는 사람이 식별하기 어려워요(자산 정보 카드에
            "이름 (id)" 형태로 함께 표시). 로딩 중엔 아직 이름을 몰라 id로 폴백해요. */}
        <h1 className="text-2xl font-bold tracking-tight">
          자산 상세 판정{" "}
          <span className="text-xl text-muted-foreground">
            {gates?.asset.name || recordId}
          </span>
        </h1>
        {gates && (
          <div className="flex items-center gap-2">
            <ThreatReportButton recordId={recordId} assetName={gates.asset.name || recordId} size="md" />
            {scanAvailability?.enabled && (
              <ScanRunButton recordId={recordId} scanned={gates.scanned}
                trigger="manual-detail" size="md" serverScanning={serverScanning}
                onStart={() => setLocalScanning(true)}
                onDone={() => { setLocalScanning(false); mutate(); }} />
            )}
          </div>
        )}
      </div>

      {isLoading ? (
        <div className="h-40 animate-pulse rounded-xl bg-muted/50" />
      ) : error || !gates ? (
        <Card className="p-6 text-sm text-red-700">게이트 정보를 불러오지 못했어요.</Card>
      ) : (
        <ReviewBody
          gates={gates}
          scanning={scanning}
          rescanArea={rescanArea}
          canReview={canReview}
          canManageResponsibility={canManageResponsibility}
          canManageThreatReportTier={canManageThreatReportTier}
          onChanged={() => mutate()}
        />
      )}
    </div>
  );
}

function ReviewBody({
  gates,
  scanning,
  rescanArea,
  canReview,
  canManageResponsibility,
  canManageThreatReportTier,
  onChanged,
}: {
  gates: GatesResponse; scanning: boolean; rescanArea: string;
  canReview: boolean;
  canManageResponsibility: boolean;
  canManageThreatReportTier: boolean;
  onChanged: () => void | Promise<unknown>;
}) {
  const router = useRouter();
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [logGate, setLogGate] = useState<{ toolId: string; label: string } | null>(null);
  // 게이트 단계별 재시도 — 클릭한 도구 id를 담아 그 행의 버튼만 진행중 표시해요.
  const [rescanningTool, setRescanningTool] = useState<string | null>(null);
  const [rescanErr, setRescanErr] = useState<string | null>(null);
  const { success, error: toastError } = useToast();

  const summary = gates.summary;
  // 필수 게이트 미통과(자동 REJECT 또는 미스캔 대기) → 승인 시 soft override 사유 필수.
  const needsOverride = summary.verdict === "auto-reject" || summary.verdict === "pending";

  async function decide(kind: "approve" | "reject") {
    setBusy(true); setErr(null); setMsg(null);
    try {
      const assetName = gates.asset?.name || gates.record_id;
      if (kind === "reject") {
        if (!reason.trim()) { setErr("반려 사유를 입력해 주세요."); setBusy(false); return; }
        await rejectGovAsset(gates.record_id, reason.trim());
        setMsg("반려 처리됐어요.");
        success("반려 처리했어요.", assetName);
      } else {
        await approveGovAsset(gates.record_id, reason.trim());
        setMsg("승인 처리됐어요.");
        success("승인 처리했어요.", assetName);
      }
      onChanged();
      // 결정 후 큐로 복귀. 인라인 메시지는 이동과 함께 사라지지만 토스트는 남아요.
      setTimeout(() => router.push(ADMIN_QUEUE), 900);
    } catch (e) {
      const message = e instanceof ApiError ? e.message : "판정 실패";
      setErr(message);
      toastError("판정을 처리하지 못했어요.", message);
    } finally {
      setBusy(false);
    }
  }

  // 단일 게이트(도구) 부분 재시도 — 성공 시 onChanged(mutate)로 폴링을 시작해 running→done 반영.
  async function rescanTool(toolId: string) {
    setRescanningTool(toolId); setRescanErr(null);
    try {
      await rescanGate(gates.record_id, toolId);
      onChanged();
    } catch (e) {
      const message = e instanceof ApiError ? e.message : "재시도 실패";
      setRescanErr(message);
      toastError("게이트 재시도에 실패했어요.", message);
    } finally {
      setRescanningTool(null);
    }
  }

  const approveDisabled = !canReview || busy || (needsOverride && !reason.trim());
  const scanApplicability = scanApplicabilityPresentation(
    gates.asset.scan_applicability,
  );

  return (
    <div className="space-y-5">
      {/* 자산 정보 (§3-M2) — 소유·타입·태그·endpoint */}
      <AssetMetaCard asset={gates.asset} recordId={gates.record_id} />

      {/* 자동승인 보류 사유 (R3) — 무엇이 없어서 안 됐고, 무엇을 하면 풀리는지.
          사유가 없으면 렌더되지 않아요(그게 "승인됨"을 뜻하진 않아요). */}
      <ApprovalBlockNotice block={gates.approval_block} audience="admin" />

      <ResponsibilityEditor
        key={gates.record_id}
        recordId={gates.record_id}
        approvalBlockReason={gates.approval_block?.reason}
        canManage={canManageResponsibility}
        onChanged={onChanged}
      />

      {/* 보안 스캔 스텝 인디케이터 (F1) */}
      <Card className="p-5">
        <div className="mb-3 flex flex-wrap items-center gap-3">
          <span className="text-sm font-semibold">보안 스캔</span>
          <span className="text-sm text-muted-foreground">
            {scanApplicability
              ? scanApplicability.label
              : scanning
              ? "스캔 진행 중…"
              : (summary.total > 0 ? `${summary.passed}/${summary.total} 통과` : "요구 게이트 없음")}
            {!scanApplicability && !scanning && " · "}
            {!scanApplicability && !scanning && (rescanArea
              ? `${areaLabel(rescanArea)} 재시도 중…`
              : (gates.scanned ? "스캔 완료" : "미스캔"))}
          </span>
          {!scanApplicability && !scanning && <RiskBadge risk={gates.risk} />}
        </div>
        {gates.stages.length > 0 && (
          <div className="flex flex-wrap items-center gap-1.5">
            {gates.stages.map((s, i) => {
              // 전체 스캔 중이면 전 단계 running. 부분 재시도면 그 area 행만(서버 stages가
              // 이미 running을 담지만, 낙관적 표시를 위해 프론트도 같은 규칙을 적용).
              const state = scanning || (rescanArea && s.area === rescanArea)
                ? "running" : s.state;
              return (
                <span key={s.tool_id} className="flex items-center gap-1.5">
                  {i > 0 && <span className="text-slate-300">━</span>}
                  <button
                    type="button"
                    onClick={() => setLogGate({ toolId: s.tool_id, label: areaLabel(s.area) })}
                    className="flex items-center gap-1.5 rounded px-1 py-0.5 transition-colors hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                    title={`${areaLabel(s.area)} 로그 보기`}
                  >
                    <span className={cn("text-sm", state === "running" && "animate-pulse", {
                      pass: "text-emerald-600", fail: "text-red-600",
                      running: "text-blue-500", pending: "text-slate-400", not_run: "text-slate-300",
                      unknown: "text-amber-600",
                    }[state] ?? "text-slate-400")}>●</span>
                    <span className="text-[11px] text-muted-foreground">{areaLabel(s.area)}</span>
                  </button>
                </span>
              );
            })}
          </div>
        )}
      </Card>

      {/* 게이트 파이프라인 (F2) */}
      <Card className="p-5">
        <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
          <span className="text-sm font-semibold">게이트 파이프라인</span>
          <div className="flex flex-wrap items-center justify-end gap-3 text-xs text-muted-foreground">
            <span>
              게이트 스캔 등급{" "}
              <span className="font-medium text-foreground">
                {tierLabel(gates.tier)}
              </span>
              <span className="ml-1">(자산 타입 설정)</span>
            </span>
            <ThreatReportTierSelect
              key={`${gates.record_id}:${gates.threat_report_tier}`}
              recordId={gates.record_id}
              tier={gates.threat_report_tier}
              canManage={canManageThreatReportTier}
              onChanged={onChanged}
            />
          </div>
        </div>
        <p className="mb-4 text-xs text-muted-foreground">
          이 값은 위협리포트 라벨에만 쓰여요. 게이트 판정·승인 큐 필터·자동승인은
          자산 «타입» 설정을 따라요.
        </p>
        {gates.stages.length === 0 ? (
          <p className="py-6 text-center text-sm text-muted-foreground">
            {scanApplicability?.label ?? "이 등급이 요구하는 게이트가 없어요."}
          </p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[300px] border-collapse text-sm">
              <thead>
                <tr className="border-b border-border text-left text-xs text-muted-foreground">
                  <th className="py-2 pr-4 font-medium">단계</th>
                  <th className="py-2 pr-4 font-medium">도구</th>
                  <th className="py-2 pr-4 font-medium">적용</th>
                  <th className="py-2 pr-4 font-medium">상태</th>
                  <th className="py-2 pr-4 font-medium">검출</th>
                  <th className="py-2 font-medium text-right">재시도</th>
                </tr>
              </thead>
              <tbody>
                {gates.stages.map((s, i) => {
                  const notApplicable = s.state === "not_applicable";
                  // 이 행이 진행중인가 — 전체 스캔이거나, 이 area가 부분 재시도 대상일 때만.
                  const rowRunning = scanning || (!!rescanArea && s.area === rescanArea);
                  // 재시도 비활성: 스캔 진행 중(전체/부분) / 해당 없음(재시도 무의미) / 권한 없음.
                  const rescanDisabled = scanning || !!rescanArea || notApplicable || !canReview
                    || rescanningTool !== null;
                  return (
                    <tr key={s.tool_id} className="border-b border-border last:border-0">
                      <td className="py-2.5 pr-4 text-muted-foreground">{i + 1}. {areaLabel(s.area)}</td>
                      <td className="py-2.5 pr-4 font-medium">{s.tool_name}</td>
                      <td className="py-2.5 pr-4 text-muted-foreground">{enforcementLabel(s.enforcement)}</td>
                      <td className="py-2.5 pr-4"><GateStatusDot state={rowRunning ? "running" : s.state} /></td>
                      <td className="py-2.5 pr-4 text-muted-foreground">
                        {rowRunning ? "…" : (s.findings_count > 0 ? `${s.findings_count} findings` : "—")}
                      </td>
                      <td className="py-2.5 text-right whitespace-nowrap">
                        {notApplicable ? (
                          <span className="text-[11px] text-muted-foreground"
                            title="이 자산엔 이 도구의 검사 대상이 없어요(소스 없는 자산의 소스 기반 도구).">
                            해당 없음
                          </span>
                        ) : (
                          <Button
                            variant="outline"
                            size="sm"
                            disabled={rescanDisabled}
                            onClick={() => rescanTool(s.tool_id)}
                            className="whitespace-nowrap"
                          >
                            {rescanningTool === s.tool_id || rowRunning ? "재시도 중…" : "재시도"}
                          </Button>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
        {rescanErr && <p className="mt-2 text-sm text-red-600">{rescanErr}</p>}
        {!canReview && gates.stages.length > 0 && (
          <p className="mt-2 text-[11px] text-muted-foreground">재시도는 reviewer/admin 권한이 필요해요.</p>
        )}
      </Card>

      <OverlapReviewCard
        overlap={gates.overlap}
        recordId={gates.record_id}
        canReview={canReview}
        onChanged={onChanged}
      />

      {/* 검출 위협 (F4) — 재스캔 중엔 이전 결과를 감춰요. */}
      {!scanning && gates.findings.length > 0 && (
        <Card className="p-5">
          <div className="mb-3 text-sm font-semibold">검출 위협 ({gates.findings.length})</div>
          <ul className="space-y-2">
            {gates.findings.map((f, i) => (
              <li key={i} className="rounded-lg border border-red-100 bg-red-50 p-3 text-[13px]">
                <span className="font-semibold text-red-800">{String(f.code ?? "UNKNOWN")}</span>
                <span className="ml-2 text-red-600">{String(f.severity ?? "")}</span>
                {f.detail ? <div className="mt-0.5 text-red-700">{String(f.detail)}</div> : null}
                {f.location ? <div className="mt-0.5 font-mono text-[11px] text-red-500">{String(f.location)}</div> : null}
              </li>
            ))}
          </ul>
        </Card>
      )}

      {/* 결정 (F7) */}
      <Card className="p-5">
        <div className="mb-2 text-sm font-semibold">결정</div>
        {/* LLM 의견 — 경고 게이트(llm-judge)가 fail이면 자동승인 대신 사람 판단으로 넘겨요.
            판단 근거를 여기 모아 보여줘, 심사자가 스캔 결과를 따로 뒤지지 않게 해요. */}
        <LlmOpinion stages={gates.stages} findings={gates.findings} />
        {needsOverride && (
          <p className="mb-2 text-[13px] text-amber-700">
            필수 게이트가 통과되지 않았어요. 이 상태로 승인하려면 사유가 필요해요(soft override, 감사에 기록).
          </p>
        )}
        <textarea
          value={reason}
          onChange={(e) => setReason(e.target.value)}
          placeholder={needsOverride ? "승인 사유 (필수) / 반려 사유" : "반려 사유 (반려 시 필수)"}
          rows={2}
          className="mb-3 w-full rounded-lg border border-input bg-card px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        />
        {err && <div className="mb-2 text-sm text-red-600">{err}</div>}
        {msg && <div className="mb-2 text-sm text-emerald-700">{msg}</div>}
        <div className="flex justify-end gap-2">
          <Button variant="outline" disabled={!canReview || busy}
            className="text-red-600" onClick={() => decide("reject")}>
            반려 REJECT
          </Button>
          <Button disabled={approveDisabled} onClick={() => decide("approve")}>
            승인 APPROVE
          </Button>
        </div>
        {!canReview && (
          <p className="mt-2 text-right text-[11px] text-muted-foreground">판정은 reviewer/admin 권한이 필요해요.</p>
        )}
      </Card>
      {logGate && (
        <GateLogModal
          recordId={gates.record_id}
          toolId={logGate.toolId}
          gateLabel={logGate.label}
          onClose={() => setLogGate(null)}
        />
      )}
    </div>
  );
}

function ThreatReportTierSelect({
  recordId,
  tier,
  canManage,
  onChanged,
}: {
  recordId: string;
  tier: GovTier;
  canManage: boolean;
  onChanged: () => void | Promise<unknown>;
}) {
  const [selected, setSelected] = useState(tier);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function update(nextTier: GovTier) {
    const previousTier = selected;
    setSelected(nextTier);
    setBusy(true);
    setError(null);
    try {
      const updated = await patchGovTargetTier(recordId, nextTier);
      setSelected(updated.target_tier);
      void Promise.resolve().then(onChanged).catch(() => undefined);
    } catch (caught) {
      setSelected(previousTier);
      setError(
        caught instanceof ApiError
          ? caught.message
          : "위협리포트 라벨을 저장하지 못했어요.",
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex flex-col items-end gap-1">
      <label className="flex items-center gap-2">
        <span>위협리포트 라벨</span>
        <Select
          aria-label="위협리포트 라벨"
          value={selected}
          disabled={!canManage || busy}
          onChange={(event) => {
            void update(event.target.value as GovTier);
          }}
          className="h-8 min-w-24 py-0 text-xs"
        >
          {GOV_TIER_VALUES.map((value) => (
            <option key={value} value={value}>
              {tierLabel(value)}
            </option>
          ))}
        </Select>
      </label>
      {error && (
        <span className="text-xs text-red-600" role="alert">
          {error}
        </span>
      )}
      {!canManage && (
        <span className="text-[11px] text-muted-foreground">
          변경은 admin 권한이 필요해요.
        </span>
      )}
    </div>
  );
}

const OVERLAP_BAND_LABELS = {
  high: "높음",
  medium: "중간",
  low: "낮음",
} as const;

function overlapTone(band: OverlapReview["band"]): TrustTone {
  if (band === "high") return "danger";
  if (band === "medium") return "warning";
  return "neutral";
}

function overlapStatusLabel(overlap: OverlapReview): string {
  if (overlap.state === "not_reviewed") return "중복검토 안 함";
  if (overlap.state === "reviewing") return "검토 중";
  if (overlap.state === "failed") return "검토 실패";
  return overlap.count > 0 ? `중복 후보 ${overlap.count}건` : "중복 없음";
}

function OverlapReviewCard({
  overlap,
  recordId,
  canReview,
  onChanged,
}: {
  overlap: OverlapReview;
  recordId: string;
  canReview: boolean;
  onChanged: () => void | Promise<unknown>;
}) {
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const reviewing = running || overlap.state === "reviewing";
  const presentation = overlapPresentation({
    overlap_state: overlap.state,
    overlap_count: overlap.count,
    overlap_band: overlap.band,
  });
  const statusTone = presentation?.tone ?? "neutral";

  async function review() {
    setRunning(true);
    setError(null);
    try {
      await runOverlapReview(recordId);
      await onChanged();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "중복검토에 실패했어요.");
    } finally {
      setRunning(false);
    }
  }

  return (
    <Card className="p-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-sm font-semibold">중복검토</span>
            <Badge
              variant="outline"
              className={cn(TONE_CLASSES[statusTone], reviewing && "animate-pulse")}
            >
              {reviewing ? "검토 중" : overlapStatusLabel(overlap)}
            </Badge>
          </div>
          {overlap.state === "reviewed" && overlap.compared !== undefined && (
            <p className="mt-1 text-xs text-muted-foreground">
              {overlap.compared}건과 비교
            </p>
          )}
        </div>
        <Button
          type="button"
          variant="outline"
          size="sm"
          disabled={!canReview || reviewing}
          onClick={review}
        >
          {reviewing ? "검토 중…" : "중복검토 수행"}
        </Button>
      </div>

      {overlap.state === "not_reviewed" && (
        <p className="mt-3 text-sm text-muted-foreground">
          아직 중복검토를 수행하지 않았어요.
        </p>
      )}
      {overlap.state === "failed" && overlap.error && (
        <p className="mt-3 text-sm text-red-700">{overlap.error}</p>
      )}
      {overlap.state === "reviewed" && overlap.candidates.length === 0 && (
        <p className="mt-3 text-sm text-muted-foreground">
          비교한 자산에서 중복 후보를 찾지 못했어요.
        </p>
      )}
      {overlap.candidates.length > 0 && (
        <div className="mt-4 divide-y divide-border border-y border-border">
          {overlap.candidates.map((candidate) => {
            const tone = overlapTone(candidate.band);
            return (
              <div key={candidate.record_id} className="py-3">
                <div className="flex flex-wrap items-center gap-2">
                  <Link
                    href={`/catalog/assets/${encodeURIComponent(candidate.record_id)}`}
                    className="text-sm font-semibold text-foreground hover:underline"
                  >
                    {candidate.name || candidate.record_id}
                  </Link>
                  <span className="text-xs text-muted-foreground">
                    {candidate.asset_type || "유형 미상"}
                    {candidate.version ? ` · v${candidate.version}` : ""}
                  </span>
                  <span className="ml-auto text-xs font-medium text-foreground">
                    {candidate.score}점
                  </span>
                  <Badge
                    variant="outline"
                    className={TONE_CLASSES[tone]}
                  >
                    {OVERLAP_BAND_LABELS[candidate.band]}
                  </Badge>
                </div>
                <p className="mt-1.5 text-[13px] text-muted-foreground">
                  <span className="font-medium text-foreground">근거</span>
                  {" · "}
                  {candidate.reasons.length > 0
                    ? candidate.reasons.join(" · ")
                    : "근거 정보 없음"}
                </p>
              </div>
            );
          })}
        </div>
      )}
      {error && (
        <p className="mt-2 text-sm text-red-600" role="alert">
          {error}
        </p>
      )}
      {!canReview && (
        <p className="mt-2 text-[11px] text-muted-foreground">
          중복검토 수행은 reviewer/admin 권한이 필요해요.
        </p>
      )}
    </Card>
  );
}

// LLM 의견 (결정 카드) — llm-judge(agent_intent) 게이트의 판정을 심사자에게 요약해요.
//
// 왜 필요한가: 경고(warn) 게이트가 fail이면 자동승인하지 않고 pending으로 두어 사람이
// 판단하게 해요(2026-07-28 결정). 그때 "무엇을 근거로 판단하라는 건지"가 결정 카드에
// 있어야 심사자가 스캔 결과를 따로 뒤지지 않아요. 특히 소스 없는 connect형 자산은
// llm-judge가 유일한 실효 검사라 이 의견이 판단의 전부예요.
function LlmOpinion({
  stages, findings,
}: {
  stages: import("@/lib/api").GateStage[];
  findings: Record<string, unknown>[];
}) {
  const judge = stages.find((s) => s.area === "agent_intent");
  if (!judge) return null;   // 이 등급에 llm-judge 게이트가 없으면 표시 안 해요.

  // agent_intent 영역 finding = THREAT_* 코드 (gate._CODE_PREFIX_TO_AREA 규약).
  const threats = findings.filter((f) => String(f.code ?? "").startsWith("THREAT_"));

  const box = "mb-3 rounded-lg border p-3 text-[13px]";
  if (judge.state === "fail") {
    // 위협 상세는 위 "검출 위협" 카드가 이미 전부 보여줘요. 여기선 되풀이하지 않고,
    // 판단에 필요한 것만 — 무엇이 몇 건인지(요약), 왜 사람에게 왔는지, 무엇을 권하는지.
    const bySeverity = threats.reduce<Record<string, number>>((acc, f) => {
      const s = String(f.severity ?? "unknown");
      acc[s] = (acc[s] ?? 0) + 1;
      return acc;
    }, {});
    const sevText = ["high", "medium", "low"]
      .filter((s) => bySeverity[s])
      .map((s) => `${s} ${bySeverity[s]}건`)
      .join(" · ");
    const codes = [...new Set(threats.map((f) => String(f.code ?? "")))];
    const isWarn = judge.enforcement === "warn";
    return (
      <div className={cn(box, isWarn ? "border-amber-200 bg-amber-50" : "border-red-200 bg-red-50")}>
        <div className={cn("font-semibold", isWarn ? "text-amber-900" : "text-red-900")}>
          LLM 의견 — 위협 {threats.length}건{sevText ? ` (${sevText})` : ""}
        </div>
        <p className={cn("mt-1", isWarn ? "text-amber-800" : "text-red-800")}>
          {isWarn
            ? "경고 게이트라 자동 반려는 아니지만, 그냥 통과시키지도 않았어요 — 판단을 위해 심사로 넘겼어요. 위 ‘검출 위협’의 근거를 보고 결정해 주세요."
            : "필수 게이트가 위협을 검출해 자동 반려 대상이에요. 승인하려면 사유가 필요해요(감사 기록)."}
        </p>
        <div className="mt-2 flex flex-wrap gap-1.5">
          {codes.map((c) => (
            <span key={c}
              className={cn("rounded-full px-2 py-0.5 font-mono text-[11px]",
                isWarn ? "bg-amber-100 text-amber-900" : "bg-red-100 text-red-900")}>
              {c}
            </span>
          ))}
        </div>
      </div>
    );
  }
  if (judge.state === "pass") {
    return (
      <div className={cn(box, "border-emerald-200 bg-emerald-50 text-emerald-800")}>
        <span className="font-semibold text-emerald-900">LLM 의견 — 위협 없음</span>
        <span className="ml-2">자산 설명·도구 정의를 검사했고 위협 신호를 찾지 못했어요.</span>
      </div>
    );
  }
  if (judge.state === "not_run") {
    return (
      <div className={cn(box, "border-slate-200 bg-slate-50 text-slate-600")}>
        <span className="font-semibold text-slate-700">LLM 의견 — 미실행</span>
        <span className="ml-2">
          아직 판정이 없어요. 스캔을 실행하면 자산 설명·도구 정의 기반 위협 판정이 채워져요.
        </span>
      </div>
    );
  }
  return null;   // pending/running/not_applicable/unknown — 표시할 의견 없음
}

// 자산 표시용 메타 카드 (§3-M2) — 소유·타입·태그·endpoint·버전. 게이트 판정과 독립인 참고 정보.
function AssetMetaCard({ asset, recordId }: {
  asset: import("@/lib/api").GovAssetMeta; recordId: string;
}) {
  const ownerIsEmail = isEmailOwnerId(asset.owner_user);
  const { data: ownerUser, error: ownerError, isLoading: ownerLoading } = useSWR(
    asset.owner_user && !ownerIsEmail
      ? ["admin/identity/user", asset.owner_user]
      : null,
    () => getCognitoUser(asset.owner_user),
  );
  const owner = ownerPresentation(
    asset.owner_user,
    ownerUser
      ? { state: "resolved", user: ownerUser }
      : ownerLoading
        ? { state: "loading" }
        : { state: "unknown" },
  );
  const dtype = asset.descriptor_type || "—";
  return (
    <Card className="p-5">
      <div className="mb-3 text-sm font-semibold">자산 정보</div>
      <dl className="grid grid-cols-1 gap-x-6 gap-y-2 text-sm sm:grid-cols-2">
        {/* 제목이 자산명만 보여주므로 record_id는 여기서 함께 노출해요(지원·문의 시 식별자). */}
        <AssetNameRow name={asset.name} recordId={recordId} />
        <MetaRow label="타입" value={dtype} />
        <div>
          <dt className="text-xs text-muted-foreground">등록자</dt>
          <dd className="mt-0.5 break-all text-foreground" title={asset.owner_user}>
            {owner.label}
          </dd>
          <dd className={cn(
            "mt-0.5 text-xs",
            owner.state === "unknown" || ownerError
              ? "text-amber-700"
              : "text-muted-foreground",
          )}>
            {owner.detail}
          </dd>
        </div>
        <div>
          <dt className="text-xs text-muted-foreground">자산 상태</dt>
          <dd className="mt-1">
            <Badge variant="type" className={registryStatusBadgeClass(asset.status)}>
              {registryStatusLabel(asset.status)}
            </Badge>
          </dd>
        </div>
        <MetaRow label="버전" value={asset.version || "—"} />
        {asset.endpoint && <EndpointRow value={asset.endpoint} />}
        <div className="sm:col-span-2">
          <dt className="text-xs text-muted-foreground">태그</dt>
          <dd className="mt-1 flex flex-wrap gap-1.5">
            {asset.tags.length === 0 ? (
              <span className="text-sm text-muted-foreground">—</span>
            ) : (
              asset.tags.map((t) => (
                <span key={t} className="rounded-full bg-slate-100 px-2 py-0.5 text-[11px] text-slate-600">
                  {t}
                </span>
              ))
            )}
          </dd>
        </div>
      </dl>
    </Card>
  );
}

// 이름 행 — "자산이름 (record_id)". id는 mono·뮤트로 보조 정보임을 드러내요.
// 이름이 없거나 id와 같으면(백엔드 _asset_meta가 조회 실패 시 name=record_id로 폴백)
// id만 한 번 보여줘 "abc (abc)" 중복·빈 괄호를 피해요.
function AssetNameRow({ name, recordId }: { name: string; recordId: string }) {
  const showId = !!name && name !== recordId;
  return (
    <div>
      <dt className="text-xs text-muted-foreground">이름</dt>
      <dd className="mt-0.5 break-all text-foreground">
        {name || recordId}
        {showId && (
          <span className="ml-1.5 font-mono text-[13px] text-muted-foreground">({recordId})</span>
        )}
      </dd>
    </div>
  );
}

function MetaRow({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div>
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd className={cn("mt-0.5 break-all text-foreground", mono && "font-mono text-[13px]")}>{value}</dd>
    </div>
  );
}

// endpoint 전용 행 — 한 줄로 표시(넘치면 가로 스크롤) + 복사 버튼. sm:col-span-2 로 전체 폭 사용.
// 복사 피드백 상태(useState)를 쓰려고 별도 하위 컴포넌트로 분리했어요(훅 규칙 준수).
function EndpointRow({ value }: { value: string }) {
  const [copied, setCopied] = useState(false);

  async function copy() {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
      // 잠깐 뒤 "복사됨" 표시를 원래대로 되돌려요.
      setTimeout(() => setCopied(false), 1500);
    } catch {
      // 클립보드 접근 실패(권한/비보안 컨텍스트) 시 조용히 무시해요.
    }
  }

  return (
    <div className="sm:col-span-2">
      <dt className="text-xs text-muted-foreground">Endpoint</dt>
      <dd className="mt-1 flex items-center gap-2">
        <input
          type="text"
          readOnly
          value={value}
          onFocus={(e) => e.currentTarget.select()}
          className="min-w-0 flex-1 whitespace-nowrap overflow-x-auto rounded-lg border border-input bg-muted/40 px-2.5 py-1.5 font-mono text-xs text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        />
        <Button
          type="button"
          variant="outline"
          size="sm"
          onClick={copy}
          className="shrink-0"
        >
          {copied ? "복사됨 ✓" : "복사"}
        </Button>
      </dd>
    </div>
  );
}
