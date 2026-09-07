"use client";

import { useState } from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import useSWR from "swr";
import { ADMIN_NAV, ADMIN_BASE, type AdminNavItem } from "@/lib/adminNav";
import { getGovMe, type GovMe } from "@/lib/api";
import { Icon } from "@/components/ui/icon";
import { cn } from "@/lib/ui";

// 관리자 콘솔 전용 사이드바 (반응형).
// - 데스크톱(lg+): 좌측 고정 사이드바 212px.
// - 모바일/태블릿(<lg): 상단 바 + 햄버거 → 슬라이드 드로어.
// 일반 포탈 Sidebar 와 달리 상단바가 없고, 메뉴는 텍스트+인라인 SVG 아이콘만 써요.
export function AdminSidebar() {
  const pathname = usePathname() || "/";
  const [drawerOpen, setDrawerOpen] = useState(false);

  return (
    <>
      {/* 모바일 상단 바 (<lg 에서만) */}
      <div className="flex items-center gap-3 border-b border-border bg-[#fcfdfe] px-4 py-3 lg:hidden">
        <button
          onClick={() => setDrawerOpen(true)}
          className="rounded-lg p-1.5 text-muted-foreground hover:bg-accent"
          aria-label="메뉴 열기"
        >
          <Icon name="menu" size={20} />
        </button>
        <Link href={ADMIN_BASE} className="text-[15px] font-extrabold tracking-tight">
          관리자 콘솔
        </Link>
      </div>

      {/* 데스크톱 고정 사이드바 (lg+) */}
      <div className="hidden lg:block">
        <SidebarNav pathname={pathname} />
      </div>

      {/* 모바일 드로어 (<lg) */}
      {drawerOpen && (
        <div className="fixed inset-0 z-50 lg:hidden" onClick={() => setDrawerOpen(false)}>
          <div className="absolute inset-0 bg-black/40" />
          <div
            className="absolute left-0 top-0 h-full"
            onClick={(e) => e.stopPropagation()}
          >
            <SidebarNav pathname={pathname} onNavigate={() => setDrawerOpen(false)} />
          </div>
        </div>
      )}
    </>
  );
}

// 사이드바 본체 — 데스크톱 고정·모바일 드로어 양쪽에서 재사용.
function SidebarNav({
  pathname,
  onNavigate,
}: {
  pathname: string;
  onNavigate?: () => void;
}) {
  return (
    <nav className="flex h-full w-[212px] shrink-0 flex-col overflow-y-auto border-r border-border bg-[#fcfdfe] p-3.5">
      <div className="mb-1 px-2.5 pt-1">
        <Link
          href={ADMIN_BASE}
          onClick={onNavigate}
          className="text-base font-extrabold tracking-tight"
        >
          관리자 콘솔
        </Link>
        <div className="mt-0.5 text-[11px] text-muted-foreground">Agora Admin</div>
      </div>

      {ADMIN_NAV.map((section) => (
        <div key={section.key}>
          <p className="mb-1.5 mt-4 px-1.5 text-[10px] font-bold uppercase tracking-[0.06em] text-slate-400">
            {section.label}
          </p>
          {section.items.map((item) => (
            <NavLink
              key={item.href}
              item={item}
              pathname={pathname}
              onNavigate={onNavigate}
            />
          ))}
        </div>
      ))}

      <SidebarFooter />
    </nav>
  );
}

// 사이드바 항목 하나. 활성 판정은 base(콘솔 홈)만 정확 일치, 나머지는 prefix 예요.
// base 를 prefix 로 보면 /admin 홈이 모든 하위 경로에서 활성으로 오탐돼요.
// /admin/queue/[id] 는 queue 항목의 prefix 에 걸려서 별도 처리가 필요 없어요.
function NavLink({
  item,
  pathname,
  onNavigate,
}: {
  item: AdminNavItem;
  pathname: string;
  onNavigate?: () => void;
}) {
  const matchingHref = ADMIN_NAV
    .flatMap((section) => section.items)
    .map((candidate) => candidate.href)
    .filter((href) =>
      href === ADMIN_BASE
        ? pathname === ADMIN_BASE
        : pathname === href || pathname.startsWith(href + "/")
    )
    .sort((left, right) => right.length - left.length)[0];
  const active = matchingHref === item.href;

  return (
    <Link
      href={item.href}
      onClick={onNavigate}
      aria-current={active ? "page" : undefined}
      className={cn(
        "mb-0.5 flex items-center gap-2.5 rounded-lg px-2.5 py-2 text-[13px]",
        active
          ? "bg-[#e0edff] font-semibold text-blue-800"
          : "text-muted-foreground hover:bg-accent",
      )}
    >
      <Icon name={item.icon} size={16} className="opacity-80" />
      <span className="min-w-0 truncate">{item.label}</span>
      {item.pending && (
        <span
          title="백엔드 준비 중"
          aria-label="백엔드 준비 중"
          className="ml-auto h-1.5 w-1.5 shrink-0 rounded-full bg-amber-400"
        />
      )}
    </Link>
  );
}

// 스캐너 모드 → 사람이 읽을 라벨 + 어디서 실행되는지. 모드는 서버 env(AGORA_SCANNER)로
// 정해져요(런타임 표시 전용, 화면에서 못 바꿈). 실 격리 스캔은 stepfn/fargate뿐이고
// static/noop은 로컬 프로세스 안에서 돌아 실 AWS 스캔이 아니에요.
const SCANNER_LABELS: Record<string, { label: string; where: string; real: boolean }> = {
  static: { label: "Static", where: "로컬 프로세스 (실 AWS 아님)", real: false },
  noop: { label: "Noop", where: "검사 안 함", real: false },
  stepfn: { label: "Step Functions", where: "실 AWS 격리 스캔", real: true },
  fargate: { label: "Fargate", where: "실 AWS 격리 스캔", real: true },
};

// 사이드바 하단 — 현재 role + 스캐너 모드 배지.
function SidebarFooter() {
  const { data: me } = useSWR<GovMe>("gov/me", getGovMe);
  const role = me?.roles?.[0] ?? "…";
  const mode = me?.mode;
  const scanner = mode?.scanner ?? "";
  const info = SCANNER_LABELS[scanner] ?? { label: scanner, where: "", real: false };

  return (
    // pb-12: 좌하단 Next.js dev indicator("N" 버튼)에 안 가리도록 하단 여백 확보(local 개발용).
    <div className="mt-auto border-t border-border px-2.5 pt-3 pb-12 text-[11px] text-muted-foreground">
      <div className="font-medium text-foreground">role: {role}</div>
      {mode && (
        <div className="mt-1.5 space-y-1">
          <div className="flex flex-wrap items-center gap-1">
            <span className="text-muted-foreground">스캔 모드</span>
            <span className={cn(
              "rounded px-1.5 py-0.5 font-medium",
              info.real ? "bg-blue-100 text-blue-800" : "bg-amber-100 text-amber-800",
            )}>
              {info.label}
            </span>
          </div>
          {info.where && <div className="text-[10px] leading-tight">{info.where}</div>}
          {/* local 개발 편의용 표시 — 모드 전환은 서버 env(AGORA_SCANNER)로만. 운영 배포엔 불필요. */}
          <div className="text-[10px] leading-tight text-muted-foreground/70">
            · 서버 env 기준 · local 개발용
          </div>
        </div>
      )}
    </div>
  );
}
