"use client";

import { useState } from "react";
import useSWR from "swr";
import {
  ApiError,
  getGovMe,
  getGovSettings,
  putGovSettings,
  type ConsoleSettings,
  type GovMe,
} from "@/lib/api";
import { Card } from "@/components/ui/card";
import { Select } from "@/components/ui/input";
import { useConfirm } from "@/components/ui/confirm-dialog";
import { cn } from "@/lib/ui";
import { TIERS, assetTypeLabel } from "@/lib/governance";

// 콘솔 설정 화면 (§3-M7). 자동 스캔 토글 + 릴리스 자동감지 주기.
// Admin 전용 — reviewer 는 읽기만(토글 비활성).
const RATE_OPTIONS: { value: string; label: string }[] = [
  { value: "P1D", label: "매일" },
  { value: "P1W", label: "1주 (기본)" },
  { value: "P2W", label: "2주" },
  { value: "P1M", label: "1개월" },
];

const JUDGE_MODELS: { value: string; label: string }[] = [
  { value: "haiku-4-5", label: "Haiku 4.5" },
  { value: "sonnet-4-6", label: "Sonnet 4.6" },
  { value: "sonnet-5", label: "Sonnet 5" },
];

export function SettingsClient() {
  const { data: me } = useSWR<GovMe>("gov/me", getGovMe);
  const {
    data: settings,
    error,
    isLoading,
    mutate,
  } = useSWR<ConsoleSettings>("gov/settings", getGovSettings);

  const canWrite = me?.can.tier_write ?? false; // settings PUT = admin only

  if (isLoading) {
    return <SettingsSkeleton />;
  }
  if (error || !settings) {
    return (
      <div>
        <Header />
        <Card className="max-w-2xl p-6 text-sm text-red-700">
          설정을 불러오지 못했어요. API 서버(:9100)가 떠 있는지 확인해 주세요.
        </Card>
      </div>
    );
  }

  return (
    <div>
      <Header />
      <SettingsForm settings={settings} canWrite={canWrite} onSaved={() => mutate()} />
    </div>
  );
}

function Header() {
  return (
    <div className="mb-6">
      <h1 className="text-2xl font-bold tracking-tight">설정</h1>
    </div>
  );
}

function SettingsForm({
  settings,
  canWrite,
  onSaved,
}: {
  settings: ConsoleSettings;
  canWrite: boolean;
  onSaved: () => void;
}) {
  const [autoScan, setAutoScan] = useState(settings.auto_scan);
  const [rate, setRate] = useState(settings.auto_detect_rate || "P1W");
  const [tierMap, setTierMap] = useState<Record<string, string>>(
    settings.asset_tier_map || { skill: "minimal", mcp: "standard", agent: "strong" },
  );
  const [judgeMap, setJudgeMap] = useState<Record<string, string>>(
    settings.judge_model_map || { minimal: "haiku-4-5", standard: "sonnet-4-6", strong: "sonnet-5" },
  );
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [savedAt, setSavedAt] = useState<string>(settings.updated_at);
  const [savedBy, setSavedBy] = useState<string>(settings.updated_by);
  const { confirm, dialog } = useConfirm();

  const dirty = autoScan !== settings.auto_scan || rate !== (settings.auto_detect_rate || "P1W");

  async function save(next: { auto_scan?: boolean; auto_detect_rate?: string; asset_tier_map?: Record<string, string>; judge_model_map?: Record<string, string> }) {
    setSaving(true);
    setError(null);
    try {
      const updated = await putGovSettings(next);
      setSavedAt(updated.updated_at);
      setSavedBy(updated.updated_by);
      onSaved();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "저장 중 오류가 발생했어요.");
      // 실패 시 UI 상태를 서버값으로 되돌려요.
      setAutoScan(settings.auto_scan);
      setRate(settings.auto_detect_rate || "P1W");
      setTierMap(settings.asset_tier_map || { skill: "minimal", mcp: "standard", agent: "strong" });
      setJudgeMap(settings.judge_model_map || { minimal: "haiku-4-5", standard: "sonnet-4-6", strong: "sonnet-5" });
    } finally {
      setSaving(false);
    }
  }

  async function onToggle() {
    if (!canWrite || saving) return;
    const next = !autoScan;
    // 위험 토글(자동 스캔 ON)은 확인 다이얼로그 후 진행.
    if (next) {
      const ok = await confirm({
        title: "자동 스캔 켜기",
        description: "자동 스캔을 켜면 앞으로 자산이 등록될 때마다 백그라운드 스캔이 실행돼요. 켤까요?",
        confirmLabel: "켜기",
      });
      if (!ok) return;
    }
    setAutoScan(next);
    save({ auto_scan: next });
  }

  function onRateChange(value: string) {
    if (!canWrite) return;
    setRate(value);
    save({ auto_detect_rate: value });
  }

  function onTierChange(assetType: string, tier: string) {
    if (!canWrite) return;
    const next = { ...tierMap, [assetType]: tier };
    setTierMap(next);
    save({ asset_tier_map: { [assetType]: tier } });
  }

  function onJudgeModelChange(tier: string, model: string) {
    if (!canWrite) return;
    const next = { ...judgeMap, [tier]: model };
    setJudgeMap(next);
    save({ judge_model_map: { [tier]: model } });
  }

  return (
    <Card className="max-w-2xl divide-y divide-border p-0">
      {dialog}
      {/* F1 자동 스캔 토글 */}
      <div className="flex items-start justify-between gap-4 p-5">
        <div>
          <div className="text-sm font-semibold">등록 시 자동 스캔</div>
          <p className="mt-1 text-[13px] text-muted-foreground">
            기본 OFF — 켜면 자산 등록 직후 백그라운드 스캔이 실행돼요. 끄면 어드민이 승인 큐에서 수동으로 스캔해요.
          </p>
        </div>
        <button
          role="switch"
          aria-checked={autoScan}
          disabled={!canWrite || saving}
          onClick={onToggle}
          className={cn(
            "relative mt-0.5 h-6 w-11 shrink-0 rounded-full transition-colors",
            autoScan ? "bg-blue-600" : "bg-slate-300",
            (!canWrite || saving) && "cursor-not-allowed opacity-50",
          )}
        >
          <span
            className={cn(
              "absolute top-0.5 h-5 w-5 rounded-full bg-white shadow transition-transform",
              autoScan ? "translate-x-[22px]" : "translate-x-0.5",
            )}
          />
        </button>
      </div>

      {/* F2 자동감지 주기 */}
      <div className="flex items-start justify-between gap-4 p-5">
        <div>
          <div className="text-sm font-semibold">릴리스 자동감지 주기</div>
          <p className="mt-1 text-[13px] text-muted-foreground">
            등록된 도구의 upstream 새 버전을 얼마나 자주 확인할지 정해요.
          </p>
        </div>
        <Select
          value={rate}
          disabled={!canWrite}
          onChange={(e) => onRateChange(e.target.value)}
          className="mt-0.5 shrink-0"
        >
          {RATE_OPTIONS.map((r) => (
            <option key={r.value} value={r.value}>{r.label}</option>
          ))}
        </Select>
      </div>

      {/* SP-1: 에셋 타입별 스캔 등급 */}
      <div className="p-5">
        <div className="text-sm font-semibold">에셋 타입별 스캔 등급</div>
        <p className="mt-1 text-[13px] text-muted-foreground">
          에셋 타입에 따라 적용할 스캔 등급을 정해요. 스캔 실행 시 이 등급이 게이트 파이프라인을 결정해요.
        </p>
        <div className="mt-3 space-y-2">
          {["skill", "mcp", "agent"].map((at) => (
            <div key={at} className="flex items-center justify-between gap-4">
              <span className="text-sm font-medium">{assetTypeLabel(at)}</span>
              <Select
                value={tierMap[at] || "minimal"}
                disabled={!canWrite}
                onChange={(e) => onTierChange(at, e.target.value)}
                className="w-40 shrink-0"
              >
                {TIERS.map((t) => (
                  <option key={t.value} value={t.value}>{t.label}</option>
                ))}
              </Select>
            </div>
          ))}
        </div>
      </div>

      {/* SP-8: LLM-judge 모델(등급별) */}
      <div className="p-5">
        <div className="text-sm font-semibold">LLM-judge 모델(등급별)</div>
        <p className="mt-1 text-[13px] text-muted-foreground">
          각 스캔 등급에서 사용할 Bedrock LLM-judge 모델을 지정해요. Haiku → 빠름·저비용, Sonnet 5 → 높은 정확도.
        </p>
        <div className="mt-3 space-y-2">
          {["minimal", "standard", "strong"].map((tier) => (
            <div key={tier} className="flex items-center justify-between gap-4">
              <span className="text-sm font-medium">{tier}</span>
              <Select
                value={judgeMap[tier] || "haiku-4-5"}
                disabled={!canWrite}
                onChange={(e) => onJudgeModelChange(tier, e.target.value)}
                className="w-40 shrink-0"
              >
                {JUDGE_MODELS.map((m) => (
                  <option key={m.value} value={m.value}>{m.label}</option>
                ))}
              </Select>
            </div>
          ))}
        </div>
      </div>

      {/* F3 설정 감사 */}
      <div className="flex items-center justify-between gap-4 px-5 py-4 text-[12px] text-muted-foreground">
        <div>
          {error ? (
            <span className="text-red-600">{error}</span>
          ) : saving ? (
            "저장 중…"
          ) : savedAt ? (
            <>마지막 변경: <span className="font-medium text-foreground">{savedBy || "—"}</span> · {formatTs(savedAt)}</>
          ) : (
            "아직 변경 이력이 없어요."
          )}
        </div>
        {!canWrite && <span className="rounded-full bg-amber-100 px-2 py-0.5 text-amber-700">읽기 전용 (admin 아님)</span>}
        {canWrite && dirty && !saving && <span className="text-blue-600">변경사항 반영됨</span>}
      </div>
    </Card>
  );
}

// KST 기준 표시 (memory: 모든 시간 KST). ISO(UTC) → Asia/Seoul.
function formatTs(iso: string): string {
  try {
    return new Date(iso).toLocaleString("ko-KR", {
      timeZone: "Asia/Seoul",
      dateStyle: "medium",
      timeStyle: "short",
    });
  } catch {
    return iso;
  }
}

function SettingsSkeleton() {
  return (
    <div>
      <Header />
      <div className="max-w-2xl space-y-2">
        {Array.from({ length: 3 }).map((_, i) => (
          <div key={i} className="h-20 animate-pulse rounded-lg bg-muted/50" />
        ))}
      </div>
    </div>
  );
}
