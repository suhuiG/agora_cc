"use client";

import { useMemo, useRef, useState } from "react";
import {
  ApiError,
  putGovTier,
  simulateGovTier,
  type GovTool,
  type SimulateResult,
  type TierCell,
} from "@/lib/api";
import { TIERS, ENFORCEMENT_OPTIONS, areaLabel } from "@/lib/governance";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/ui";

// 등급별 구성 매트릭스 (§3-M4) — 편집 가능.
// 행=도구, 열=최소/표준/강화. 셀 드롭다운으로 enforcement(필수/경고/—) 지정.
interface TierMatrixProps {
  tools: GovTool[];
  /** tier → cells (초기값). 예: {minimal:[...], standard:[...], strong:[...]}. */
  cellsByTier: Record<string, TierCell[]>;
  canWrite: boolean;
  /** 저장 성공 후 부모가 tier 데이터를 refetch 하도록 알려요. */
  onSaved: () => void;
}

// (tier,tool)→enforcement 로컬 편집 상태. key = `${tier}:${tool_id}`.
type Draft = Record<string, string>;

const ENF_CLASS: Record<string, string> = {
  required: "border-blue-300 bg-blue-50 text-blue-800",
  warn: "border-amber-300 bg-amber-50 text-amber-800",
  off: "border-border bg-card text-muted-foreground",
};

function keyOf(tier: string, toolId: string) {
  return `${tier}:${toolId}`;
}

export function TierMatrix({ tools, cellsByTier, canWrite, onSaved }: TierMatrixProps) {
  // 초기 draft: 서버 cells → 맵. 없는 (tier,tool)은 "off".
  const initial = useMemo(() => {
    const d: Draft = {};
    for (const tier of TIERS) {
      for (const tool of tools) d[keyOf(tier.value, tool.tool_id)] = "off";
      for (const cell of cellsByTier[tier.value] ?? []) {
        d[keyOf(tier.value, cell.tool_id)] = cell.enforcement;
      }
    }
    return d;
  }, [tools, cellsByTier]);

  const [draft, setDraft] = useState<Draft>(initial);
  const [saving, setSaving] = useState(false);
  const [simulating, setSimulating] = useState(false);
  const [preview, setPreview] = useState<Record<string, SimulateResult> | null>(null);
  const [error, setError] = useState<string | null>(null);
  const draftRevision = useRef(0);

  // dirty: 초기값과 다른 셀이 하나라도 있으면.
  const dirty = useMemo(
    () => Object.keys(initial).some((k) => (draft[k] ?? "off") !== (initial[k] ?? "off")),
    [draft, initial],
  );

  function setCell(tier: string, toolId: string, value: string) {
    if (!canWrite) return;
    draftRevision.current += 1;
    setDraft((d) => ({ ...d, [keyOf(tier, toolId)]: value }));
    setPreview(null);
  }

  // draft → tier별 cells(off 제외) 로 변환.
  function cellsFor(tier: string): Partial<TierCell>[] {
    return tools
      .map((t) => ({ tier, tool_id: t.tool_id, enforcement: draft[keyOf(tier, t.tool_id)] ?? "off" }))
      .filter((c) => c.enforcement !== "off");
  }

  async function handleSave() {
    setSaving(true);
    setError(null);
    try {
      // 3등급 전부 저장(각 tier cells 통째 교체).
      await Promise.all(TIERS.map((t) => putGovTier(t.value, cellsFor(t.value))));
      setPreview(null);
      onSaved();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "저장 중 오류가 발생했어요.");
    } finally {
      setSaving(false);
    }
  }

  async function handlePreview() {
    const revision = draftRevision.current;
    setSimulating(true);
    setError(null);
    try {
      const results = await Promise.all(
        TIERS.map((tier) => simulateGovTier(tier.value, cellsFor(tier.value))),
      );
      if (draftRevision.current === revision) {
        setPreview(
          Object.fromEntries(TIERS.map((tier, index) => [tier.value, results[index]])),
        );
      }
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "미리보기를 계산하지 못했어요.");
      setPreview(null);
    } finally {
      setSimulating(false);
    }
  }

  return (
    <div>
      <div className="mb-4 flex flex-wrap items-start justify-between gap-3">
        <p className="max-w-2xl text-sm text-muted-foreground">
          각 등급에서 켤 도구를 정의해요. 필수=통과해야 승인 · 경고=실패해도 통과(기록) · —=미적용.
          이 매트릭스가 자산 상세 판정의 게이트 파이프라인을 결정해요(SoT).
        </p>
        <div className="flex shrink-0 items-center gap-2">
          <Button
            variant="outline"
            size="sm"
            disabled={!canWrite || saving || simulating || !dirty}
            onClick={handlePreview}
          >
            {simulating ? "계산 중…" : "변경 영향 확인"}
          </Button>
          <Button
            size="sm"
            disabled={!canWrite || saving || simulating || !dirty}
            onClick={handleSave}
          >
            {saving ? "저장 중…" : dirty ? "저장" : "저장됨"}
          </Button>
        </div>
      </div>

      {error && (
        <div className="mb-3 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">
          {error}
        </div>
      )}

      {preview && (
        <section
          aria-labelledby="tier-preview-title"
          className="mb-4 border-y border-border bg-muted/20 py-4"
        >
          <div className="mb-3 flex flex-wrap items-baseline justify-between gap-2 px-1">
            <h3 id="tier-preview-title" className="text-sm font-semibold text-foreground">
              변경 영향
            </h3>
            <span className="text-xs text-muted-foreground">저장 전 재판정 결과</span>
          </div>
          <div className="grid gap-0 md:grid-cols-3">
            {TIERS.map((tier, index) => {
              const result = preview[tier.value];
              if (!result) return null;
              const changed =
                result.transitions.pass_to_fail + result.transitions.fail_to_pass;
              const assetTypes = Object.entries(result.by_asset_type).filter(
                ([, counts]) =>
                  counts.affected > 0 ||
                  counts.pass_to_fail > 0 ||
                  counts.fail_to_pass > 0,
              );

              return (
                <div
                  key={tier.value}
                  className={cn(
                    "px-4 py-2",
                    index > 0 && "border-t border-border md:border-l md:border-t-0",
                  )}
                >
                  <div className="flex items-center justify-between gap-3">
                    <h4 className="text-sm font-semibold text-foreground">{tier.label}</h4>
                    <span className="text-xs text-muted-foreground">
                      {result.affected}개 영향
                    </span>
                  </div>

                  {changed === 0 ? (
                    <p className="mt-3 text-sm text-muted-foreground">
                      판정이 바뀌는 자산이 없어요.
                    </p>
                  ) : (
                    <>
                      <dl className="mt-3 grid grid-cols-2 gap-2 text-xs">
                        <div>
                          <dt className="text-muted-foreground">PASS → FAIL</dt>
                          <dd className="mt-1 text-base font-semibold text-red-700">
                            {result.transitions.pass_to_fail}
                          </dd>
                        </div>
                        <div>
                          <dt className="text-muted-foreground">FAIL → PASS</dt>
                          <dd className="mt-1 text-base font-semibold text-emerald-700">
                            {result.transitions.fail_to_pass}
                          </dd>
                        </div>
                      </dl>

                      {assetTypes.length > 0 && (
                        <div className="mt-4">
                          <p className="text-xs font-medium text-foreground">자산 유형별</p>
                          <ul className="mt-1.5 space-y-1 text-xs text-muted-foreground">
                            {assetTypes.map(([assetType, counts]) => (
                              <li key={assetType} className="flex justify-between gap-3">
                                <span>{assetType}</span>
                                <span>
                                  {counts.pass_to_fail}↓ · {counts.fail_to_pass}↑
                                </span>
                              </li>
                            ))}
                          </ul>
                        </div>
                      )}

                      {result.sample.length > 0 && (
                        <div className="mt-4">
                          <p className="text-xs font-medium text-foreground">변경 예시</p>
                          <ul className="mt-1.5 space-y-1.5 text-xs">
                            {result.sample.map((asset) => (
                              <li key={asset.record_id}>
                                <span className="font-medium text-foreground">{asset.name}</span>
                                <span className="ml-2 text-muted-foreground">
                                  {asset.old_verdict} → {asset.new_verdict}
                                </span>
                              </li>
                            ))}
                          </ul>
                        </div>
                      )}
                    </>
                  )}
                </div>
              );
            })}
          </div>
        </section>
      )}

      {/* 매트릭스 (좁은 화면 가로 스크롤) */}
      <div className="overflow-x-auto rounded-xl border border-border">
        <table className="w-full min-w-[720px] border-collapse text-sm">
          <thead>
            <tr className="border-b border-border bg-muted/40 text-left text-xs text-muted-foreground">
              <th className="px-4 py-2.5 font-medium">영역</th>
              <th className="px-4 py-2.5 font-medium">도구</th>
              {TIERS.map((t) => (
                <th key={t.value} className="px-4 py-2.5 text-center font-medium">
                  {t.label}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {tools.map((tool) => (
              <tr key={tool.tool_id} className="border-b border-border last:border-0">
                <td className="px-4 py-3 text-muted-foreground">{areaLabel(tool.area)}</td>
                <td className="px-4 py-3 font-medium text-foreground">{tool.name}</td>
                {TIERS.map((t) => {
                  const enf = draft[keyOf(t.value, tool.tool_id)] ?? "off";
                  return (
                    <td key={t.value} className="px-4 py-2.5 text-center">
                      <select
                        value={enf}
                        disabled={!canWrite}
                        onChange={(e) => setCell(t.value, tool.tool_id, e.target.value)}
                        className={cn(
                          "h-8 rounded-md border px-2 text-xs font-medium",
                          "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                          ENF_CLASS[enf] ?? ENF_CLASS.off,
                          !canWrite && "cursor-not-allowed opacity-70",
                        )}
                      >
                        {ENFORCEMENT_OPTIONS.map((o) => (
                          <option key={o.value} value={o.value}>{o.label}</option>
                        ))}
                      </select>
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <p className="mt-3 text-xs text-muted-foreground">
        {canWrite
          ? "셀을 바꾼 뒤 [저장]하세요. 통과정책·임계치는 후속 단계에서 추가돼요."
          : "읽기 전용 — 등급 편집은 admin 권한이 필요해요."}
      </p>
    </div>
  );
}
