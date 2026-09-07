import { Suspense } from "react";
import { TopBar } from "@/components/TopBar";
import { Sidebar } from "@/components/Sidebar";

// 일반 포탈 셸: 상단바 + 도메인 사이드바 + 본문.
// (거버넌스 Admin 콘솔은 (console) route group 에서 별도 셸을 써요.)
export default function PortalLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <>
      {/* TopBar 는 useSearchParams 를 쓰므로 Suspense 로 감싸 prerender 빌드 에러를 막아요. */}
      <Suspense fallback={<div className="h-[57px] border-b border-border bg-card" />}>
        <TopBar />
      </Suspense>

      <div className="flex flex-1">
        <Sidebar />
        <main className="min-w-0 flex-1 px-4 py-6 sm:px-6 sm:py-7">{children}</main>
      </div>
    </>
  );
}
