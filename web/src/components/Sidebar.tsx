"use client";

import { useState } from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import useSWR from "swr";
import { getAuthSession } from "@/lib/auth-client";
import { visibleNav, type NavDomain } from "@/lib/nav";
import { Icon } from "@/components/ui/icon";
import { cn } from "@/lib/ui";

// 도메인 중심 사이드바.
// 1차(도메인)는 굵은 헤더, 2차(기능)는 들여쓴 항목이에요.
// 현재 경로가 속한 도메인은 펼치고 active 표시, soon 도메인은 흐리게 + "예정" 뱃지.
export function Sidebar() {
  const pathname = usePathname() || "/";
  const [drawerOpen, setDrawerOpen] = useState(false);
  const { data: session } = useSWR("/api/auth/session", () => getAuthSession(), {
    shouldRetryOnError: false,
  });
  const isAdmin =
    session?.authenticated === true && session.roles?.includes("admin") === true;
  const domains = visibleNav(isAdmin);

  return (
    <>
      <button
        type="button"
        onClick={() => setDrawerOpen(true)}
        className="fixed left-3 top-[11px] z-30 rounded-lg p-1.5 text-muted-foreground hover:bg-accent lg:hidden"
        aria-label="메뉴 열기"
      >
        <Icon name="menu" size={20} />
      </button>

      <div className="hidden lg:block">
        <SidebarNav pathname={pathname} domains={domains} />
      </div>

      {drawerOpen && (
        <div
          className="fixed inset-0 z-50 lg:hidden"
          onClick={() => setDrawerOpen(false)}
        >
          <div className="absolute inset-0 bg-black/40" />
          <div
            className="absolute left-0 top-0 h-full"
            onClick={(event) => event.stopPropagation()}
          >
            <SidebarNav
              pathname={pathname}
              domains={domains}
              onNavigate={() => setDrawerOpen(false)}
            />
          </div>
        </div>
      )}
    </>
  );
}

function SidebarNav({
  pathname,
  domains,
  onNavigate,
}: {
  pathname: string;
  domains: NavDomain[];
  onNavigate?: () => void;
}) {
  return (
    <nav className="h-full w-[232px] shrink-0 overflow-y-auto border-r border-border bg-[#fcfdfe] p-3.5">
      {domains.map((domain) => {
        const isSoon = domain.status === "soon";
        // 도메인 활성 여부: 자식 href 중 하나가 현재 경로의 prefix 면 active.
        const domainActive = domain.children.some(
          (c) => pathname === c.href || pathname.startsWith(c.href + "/"),
        );

        // 도메인 헤더 내용 (아이콘 + 라벨 + 뱃지). 링크/비링크 공용.
        const headerInner = (
          <>
            <Icon name={domain.icon} size={16} className="opacity-80" />
            <span>{domain.label}</span>
            {domain.href && domain.newTab ? (
              // 런처(새 탭) 도메인은 "예정" 대신 외부 링크 아이콘으로 새 탭임을 알려요.
              <Icon name="external" size={13} className="ml-auto opacity-60" />
            ) : (
              isSoon && (
                <span className="ml-auto rounded-full bg-amber-100 px-1.5 py-0.5 text-[9px] font-semibold text-amber-700">
                  예정
                </span>
              )
            )}
          </>
        );

        const headerClass = cn(
          "flex items-center gap-2.5 rounded-lg px-2.5 py-2 text-sm font-bold",
          // 도메인 헤더는 음영 없이 글자만 진하게 — 강조는 클릭한 하위 항목에만.
          domainActive && "text-foreground",
          isSoon && "font-semibold text-muted-foreground",
          domain.href && "hover:bg-accent",
        );

        return (
          <div key={domain.key} className="mb-1.5">
            {domain.href ? (
              <a
                href={domain.href}
                target={domain.newTab ? "_blank" : undefined}
                rel={domain.newTab ? "noopener noreferrer" : undefined}
                onClick={onNavigate}
                className={headerClass}
              >
                {headerInner}
              </a>
            ) : (
              <div className={headerClass}>{headerInner}</div>
            )}

            {/* 2차 기능 — 도메인이 active 거나 live 일 때만 펼쳐요. (런처 도메인은 자식이 없어요.) */}
            {(domainActive || domain.status === "live") && (() => {
              // 현재 경로에 매치되는 자식 중 "가장 구체적인(href가 가장 긴)" 하나만 active로 봐요.
              // 이래야 /playground/initializr 에서 /playground(도구 호출)가 prefix로 오탐되지 않아요.
              const bestMatch = domain.children
                .filter((c) => pathname === c.href || pathname.startsWith(c.href + "/"))
                .sort((a, b) => b.href.length - a.href.length)[0]?.href;
              return (
              <div className="my-1 ml-[19px] border-l border-border pl-3.5">
                {domain.children.map((child, i) => {
                  const childActive = child.href === bestMatch;

                  return (
                    <Link
                      key={`${child.label}-${i}`}
                      href={child.href}
                      onClick={onNavigate}
                      className={cn(
                        "flex items-center gap-2 rounded-md px-2.5 py-1.5 text-[12.5px]",
                        childActive
                          ? "bg-accent font-semibold text-foreground"
                          : "text-muted-foreground hover:bg-accent",
                      )}
                    >
                      {child.label}
                    </Link>
                  );
                })}
              </div>
              );
            })()}
          </div>
        );
      })}
    </nav>
  );
}
