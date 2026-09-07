"use client";

import { useState } from "react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { AuthControls } from "@/components/AuthControls";
// 로컬 폴러 ON/OFF 칩 — 2026-09-06 에 상단바에서 숨겼어요. 되살리려면 아래 import 와
// 본문의 `<DevPollerToggle />` 한 줄을 함께 되살려요(파일은 지우지 않았어요).
// import { DevPollerToggle } from "@/components/DevPollerToggle";
import { Icon } from "@/components/ui/icon";

// 상단바: 로고 + 통합 검색창 + 사용자.
// 검색은 "통합" — 입력 후 제출하면 /catalog/browse?q= 로 이동해 둘러보기 그리드를 필터해요.
// useSearchParams 를 쓰므로 layout 에서 <Suspense> 로 감싸 prerender 시 빌드 에러를 막아요.
export function TopBar() {
  const router = useRouter();
  const params = useSearchParams();
  const [q, setQ] = useState(params.get("q") ?? "");

  function submit(e: React.FormEvent) {
    e.preventDefault();
    const query = q.trim();
    router.push(query ? `/catalog/browse?q=${encodeURIComponent(query)}` : "/catalog/browse");
  }

  return (
    <header className="sticky top-0 z-20 flex items-center gap-2 border-b border-border bg-card px-3 py-2.5 pl-14 sm:gap-4 sm:px-5 lg:pl-5">
      <Link href="/catalog/browse" className="text-lg font-extrabold tracking-tight">
        Agora
      </Link>

      {/* 통합 검색창 */}
      <form onSubmit={submit} className="flex min-w-0 max-w-[440px] flex-1 items-center sm:ml-2.5">
        <div className="flex h-9 w-full items-center gap-2 rounded-lg border border-input bg-card px-3 text-sm focus-within:ring-2 focus-within:ring-ring">
          <Icon name="search" size={15} className="text-muted-foreground" />
          <input
            id="agora-search"
            type="text"
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="Agent · Skill · MCP 통합 검색…"
            className="min-w-0 w-full bg-transparent text-foreground outline-none placeholder:text-muted-foreground"
          />
        </div>
      </form>

      <div className="ml-auto flex min-w-0 items-center">
        {/* 2026-09-06 (제품 오너 결정): 상단바에서 「폴러 OFF」 칩을 숨겼어요.
            ⚠️ 기능을 없앤 게 아니에요 — `DevPollerToggle` 파일과 그 API 는 그대로예요.
            로컬 실 AWS e2e 에서 폴러를 켜고 꺼야 할 때가 있어서, 그때 이 한 줄을 되살리면
            돼요(대안은 백엔드를 재기동해 폴러 상태를 바꾸는 것인데 그게 이 칩이 생긴 이유예요).
            칩이 «로컬 백엔드의 폴러 task» 만 다루고 포털 ECS `desiredCount` 는 건드리지
            않는다는 성질도 그대로예요. */}
        <AuthControls />
      </div>
    </header>
  );
}
