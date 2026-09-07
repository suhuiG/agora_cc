"use client";

import { useState } from "react";
import useSWR from "swr";
import {
  listAssetCapabilityPolicies,
  listAvailableAccessConnections,
  type AccessConnectionSummary,
  type AssetCapabilityPolicy,
} from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  EmptyState,
  InlineError,
  LoadError,
  LoadingRows,
  SectionHeading,
  StatusBadge,
  TagList,
} from "@/components/admin/access/shared";

// 자산 권한 정책 — 자산 하나의 operation 별 ⑤ `AssetCapability` 행을 **읽기 전용**으로 보여줘요.
//
// 폐기(ADR-0099 결정 3): ⑤ capability 라벨 층은 호출 인가 판정에서 빠졌어요. Gateway REQUEST
// interceptor 가 `get_asset_capability` 를 읽지 않으니 이 행은 도구를 열지도 닫지도 않아요.
// 현행 인가는 두 층이에요 — ④ agent 도구 승인과 ⑦ 사람·그룹 도구 권한.
//
// IH-162 (ADR-0112): 쓰기 액션을 **지웠어요.** 이 화면은 죽은 층을 읽기만 하던 게 아니라
// 「operation policy 추가」·「편집」·「승인」으로 **쓰기까지 성공**시켰어요. 서버 라우트
// (`PUT /api/assets/{id}/capabilities/{op}`)는 아직 살아 있어서(IH-139 잔여) 저장이 실제로
// 됐고, 죽은 층에 행이 쌓였어요. 화면 자체는 남겨요 — 기존 레코드 조회와 되돌림 경로가
// 그 행을 봐야 하거든요.
//
// ⚠️ 왜 배너를 `mode` 로 분기하지 않나: 옛 `mode` prop 은 저장소 전체에서 **항상 `"owner"`**
// 였어요(2026-09-05 grep, 호출부 1곳 — `MyAccessClient.tsx` 의 `mode="owner"`). PENDING /
// APPROVED 를 가르는 실제 판별자는 서버의 `principal.is_admin` 이에요
// (`access_router.put_asset_capability`: `APPROVED if principal.is_admin else PENDING`).
// 그래서 `mode !== "admin"` 으로 배너를 감추면 **어드민에게만 거짓**이 되고, 하필 어드민이
// 쓴 행만 `APPROVED` 로 남아 `/admin/capability-sets` 「어긋남 발견」 카운터에 익명 집계됐어요.
// 배너는 누구에게나 같은 사실이라 분기가 없어요. prop 도 함께 없앴어요 — 살아 있는 분기가
// 없는데 계약만 남기면 다음 사람이 그 분기를 근거로 다시 쓸 수 있어요.

type PolicyConnection = Pick<
  AccessConnectionSummary,
  "connection_id" | "name" | "status"
>;

export function AssetPoliciesPanel() {
  const [assetInput, setAssetInput] = useState("");
  const [assetId, setAssetId] = useState("");
  const [actionError, setActionError] = useState("");
  const { data: connections } = useSWR<PolicyConnection[]>(
    "access/connections",
    listAvailableAccessConnections,
  );
  const {
    data: policies,
    error,
    isLoading,
  } = useSWR<AssetCapabilityPolicy[]>(
    assetId ? `assets/${assetId}/capabilities` : null,
    () => listAssetCapabilityPolicies(assetId),
  );

  function load(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const value = assetInput.trim();
    if (!value) {
      setActionError("자산 ID를 입력해 주세요.");
      return;
    }
    setActionError("");
    setAssetId(value);
  }

  return (
    <div className="space-y-5">
      <SectionHeading
        title="자산 권한 정책 (인가 미사용 · 읽기 전용)"
        description="예전에 정의한 operation 별 capability 요구사항을 조회해요. 이 값은 도구 인가 판정에 쓰이지 않아요."
      />

      <div
        role="alert"
        className="rounded-lg border border-amber-300 bg-amber-50 p-3.5 text-xs text-amber-900"
      >
        <b className="block text-[13px]">
          이 층은 폐기됐어요 — 읽기 전용이에요 (ADR-0099)
        </b>
        <p className="mt-1 leading-relaxed">
          capability 라벨은 호출 인가 판정에서 빠졌어요. 여기 값을 바꿔도 어떤 도구도 열리거나
          막히지 않아서, 편집·승인 액션을 없앴어요. 남은 행은 기존 레코드 조회와 되돌림을 위한
          기록이에요.
        </p>
        <p className="mt-1 leading-relaxed">
          도구 인가는 두 층이에요 — <b>agent 의 도구 승인</b>과{" "}
          <b>사람·그룹의 도구 권한</b>. 내 도구 권한이 막혔다면 Agora 관리자에게 그 두 층을
          확인해 달라고 요청해 주세요.
        </p>
      </div>

      <form onSubmit={load} className="flex min-w-0 flex-col gap-2 sm:flex-row">
        <label htmlFor="policy-asset-id" className="sr-only">
          자산 ID
        </label>
        <Input
          id="policy-asset-id"
          value={assetInput}
          onChange={(event) => setAssetInput(event.target.value)}
          placeholder="자산 record ID"
          className="min-w-0 sm:max-w-xl"
        />
        <Button type="submit" className="shrink-0">
          정책 조회
        </Button>
      </form>
      {actionError && <InlineError message={actionError} />}

      {!assetId ? (
        <EmptyState
          title="조회할 자산을 선택해 주세요."
          description="자산 record ID를 입력하면 예전에 정의한 operation별 권한 정책을 볼 수 있어요."
        />
      ) : isLoading ? (
        <LoadingRows label="자산 정책 불러오는 중" />
      ) : error ? (
        <LoadError message="자산 정책을 불러오지 못했어요. 자산 ID를 확인해 주세요." />
      ) : (
        <>
          <div className="flex min-w-0 flex-wrap items-center justify-between gap-3 border-b border-border pb-3">
            <div className="min-w-0">
              <div className="text-xs text-muted-foreground">조회 자산</div>
              <div className="break-all text-sm font-semibold">{assetId}</div>
            </div>
          </div>

          {policies?.length === 0 ? (
            <EmptyState
              title="정의된 자산 정책이 없어요."
              description="이 층은 폐기됐으니 새로 만들 필요가 없어요."
            />
          ) : (
            <div className="divide-y divide-border rounded-lg border border-border">
              {policies?.map((policy) => {
                const connection = connections?.find(
                  (item) => item.connection_id === policy.connection_id,
                );
                return (
                  <div
                    key={policy.operation_id}
                    className="grid min-w-0 gap-3 px-4 py-4 md:grid-cols-[minmax(0,1fr)_minmax(0,1.2fr)] md:items-center"
                  >
                    <div className="min-w-0">
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="break-all text-sm font-semibold">
                          {policy.operation_id}
                        </span>
                        <StatusBadge status={policy.status} />
                      </div>
                      <p className="mt-1 break-all text-xs text-muted-foreground">
                        {connection?.name ?? policy.connection_id}
                      </p>
                    </div>
                    <TagList items={policy.required_capabilities} />
                  </div>
                );
              })}
            </div>
          )}
        </>
      )}
    </div>
  );
}
