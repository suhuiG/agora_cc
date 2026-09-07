"use client";

import { SWRConfig } from "swr";
import { API_BASE, ApiError } from "@/lib/api";
import { ToastProvider } from "@/components/ui/toast";

// 전역 SWR fetcher — 키 배열의 첫 요소를 경로로 보고 GET. 나머지 요소는
// 캐시 키 구분용(호출자가 완성된 URL을 첫 요소로 넣는 방식도 허용).
async function fetcher(key: string | readonly unknown[]): Promise<unknown> {
  const path = Array.isArray(key) ? String(key[0]) : String(key);
  const res = await fetch(path.startsWith("http") ? path : API_BASE + path);
  if (!res.ok) {
    let detail: string | undefined;
    try {
      detail = ((await res.json()) as { detail?: string })?.detail;
    } catch {
      /* non-JSON body */
    }
    throw new ApiError(detail || res.statusText, res.status);
  }
  return res.json();
}

export function Providers({ children }: { children: React.ReactNode }) {
  return (
    <SWRConfig
      value={{
        fetcher,
        revalidateOnFocus: false,
        dedupingInterval: 30000,
        keepPreviousData: true,
      }}
    >
      {/* 토스트는 SWR 안쪽에 둬요 — 데이터 갱신 후 결과를 알리는 화면이 대부분이라
          두 컨텍스트가 함께 필요해요. */}
      <ToastProvider>{children}</ToastProvider>
    </SWRConfig>
  );
}
