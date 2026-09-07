import { AdminSidebar } from "@/components/admin/AdminSidebar";
import { AdminGuard } from "@/components/admin/AdminGuard";

// 관리자 콘솔 셸: 상단바 없이 AdminSidebar + 본문.
// URL 은 /admin/* 이고 (console) route group 으로 루트 레이아웃과 분리돼
// 일반 포탈 상단바·사이드바가 붙지 않아요.
// 구 URL(/governance/admin/*)은 같은 route group 의 [[...path]] shim 이 넘겨줘요.
export default function ConsoleLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <AdminGuard>
      {/* 모바일: 세로 스택(상단바 + 본문) / 데스크톱(lg+): 가로(사이드바 + 본문). */}
      <div className="flex flex-1 flex-col lg:flex-row">
        <AdminSidebar />
        <main className="min-w-0 flex-1 px-4 py-6 sm:px-6 lg:px-7 lg:py-7">{children}</main>
      </div>
    </AdminGuard>
  );
}
