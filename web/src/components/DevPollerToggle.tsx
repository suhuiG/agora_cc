"use client";

/**
 * 로컬 배포 폴러 ON/OFF 칩 — **임시 기능이에요. GA 때 이 파일과 TopBar 의 한 줄을 지워요.**
 *
 * 왜 있나: 배포 job 폴러는 역할당 하나만 돌아야 해요(RT-02). 로컬에서 실 AWS e2e 를 할
 * 때마다 백엔드를 재기동해 폴러를 켜고 꺼야 했고, 더 나쁜 건 "job 이 왜 안 도는지" 가
 * 화면에 안 보여서 원인 찾는 데 시간이 들었어요(2026-08-29 실측).
 *
 * **로컬 백엔드의 폴러 task 만** 다뤄요. 포털 ECS `desiredCount` 는 건드리지 않아요.
 * 단일 출처는 `api/src/agora/shared/dev_poller_switch.py` 예요.
 *
 * 못 바꾸는 상태에서는 **이유를 그대로 보여줘요** — 조용히 비활성이면 무엇이 문제인지
 * 알 수 없어요(IH-50/IH-76 과 같은 규약).
 */

import useSWR from "swr";
import { useState } from "react";

import { request } from "@/lib/api/client";

type DevPollerState = {
  role: string;
  running: boolean;
  togglable: boolean;
  reason: string;
  owned_by_env: boolean;
};

async function fetchState(): Promise<DevPollerState> {
  return request<DevPollerState>("/api/dev/poller");
}

export function DevPollerToggle() {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const { data, mutate } = useSWR("dev/poller", fetchState, {
    revalidateOnFocus: false,
    // 상태가 밖에서 바뀔 수 있어요(백엔드 재기동·env 변경). 가볍게 재확인해요.
    refreshInterval: 15_000,
    shouldRetryOnError: false,
  });

  // 조회 자체가 실패하면 아무것도 안 보여줘요 — 이 칩이 없는 배포도 정상이에요.
  if (!data) return null;

  const label = data.running ? "폴러 ON" : "폴러 OFF";
  const tone = data.running
    ? "border-amber-300 bg-amber-50 text-amber-800"
    : "border-slate-300 bg-slate-50 text-slate-600";

  // `data` 를 인자로 받아요 — early return 뒤에 정의해도 클로저 안에서는 좁혀진 타입이
  // 유지되지 않아서, 호출 지점에서 넘겨줘야 TS 가 undefined 를 배제해요.
  async function toggle(state: DevPollerState) {
    if (!state.togglable || busy) return;
    setBusy(true);
    setError("");
    try {
      const next = await request<DevPollerState>("/api/dev/poller", {
        method: "PUT",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ enabled: !state.running }),
      });
      await mutate(next, { revalidate: false });
    } catch (e) {
      setError(e instanceof Error ? e.message : "바꾸지 못했어요.");
    } finally {
      setBusy(false);
    }
  }

  // 못 바꾸는 이유 + 켜져 있을 때의 위험을 title 에 실어요.
  const title = data.togglable
    ? (data.running
      ? "로컬 폴러가 배포 job 을 전진시켜요. 포털 폴러가 함께 돌면 소유자가 비결정적이에요(RT-02). 눌러서 끌 수 있어요."
      : "로컬 폴러가 꺼져 있어 배포 job 이 QUEUED 에 머물러요. 눌러서 켤 수 있어요.")
    : data.reason;

  return (
    <div className="mr-3 flex items-center gap-1.5">
      <button
        type="button"
        onClick={() => void toggle(data)}
        disabled={!data.togglable || busy}
        title={title}
        data-testid="dev-poller-toggle"
        className={
          "rounded-full border px-2.5 py-1 text-xs font-medium transition "
          + tone
          + (data.togglable
            ? " cursor-pointer hover:brightness-95"
            : " cursor-not-allowed opacity-70")
        }
      >
        {busy ? "…" : label}
        <span className="ml-1 font-normal opacity-70">{data.role || "?"}</span>
      </button>
      {!data.togglable && data.reason && (
        <span
          className="max-w-[220px] truncate text-xs text-muted-foreground"
          title={data.reason}
        >
          {data.reason}
        </span>
      )}
      {error && <span className="text-xs text-red-700" title={error}>실패</span>}
    </div>
  );
}
