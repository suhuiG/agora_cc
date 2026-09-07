// Agora 통합 포탈 내비게이션 정의 (단일 출처).
// 1차 = 현재 기능이 있는 진입점, 2차 = 그 진입점의 기능.
// 런타임·평가 라우트는 남아 있지만 기능 없는 placeholder라 nav에서는 노출하지 않아요(IH-163).
//
// 도메인 구조 출처: docs/superpowers/specs/2026-06-23-agora-portal-domains-design.md
//  1. 카탈로그      : skill·MCP·agent·app·model 발견 + 스펙/tool 설명  (검색·둘러보기·등록)
//  2. 런타임        : MCP·agent 구동 → 단일 게이트웨이 endpoint 제공 (nav 비노출)
//  3. 플레이그라운드 : 배포된 agent를 runtime ARN으로 대화 테스트하는 콘솔
//  4. 거버넌스      : 레지스트리 승인 + 보안 정책 + 사용량·예산
//  5. 평가          : 품질 점수 + 옵저버빌리티(트레이스·메트릭·비용) (nav 비노출)

/** 현재 `soon` 생산자는 0개예요. 렌더 계약 정리는 별 범위라 타입은 유지해요. */
export type NavStatus = "live" | "soon";

export interface NavChild {
  label: string;
  href: string;
  /** 관리자 역할이 확인된 사용자에게만 보여요. */
  adminOnly?: boolean;
}

export interface NavDomain {
  key: string;
  label: string;
  /** 의존성 없는 인라인 SVG path 식별자 (ICON_PATHS 키). */
  icon: string;
  status: NavStatus;
  children: NavChild[];
  /** 있으면 도메인 헤더 자체가 이 경로로 가는 링크가 돼요 (런처). */
  href?: string;
  /** href 를 새 탭/창에서 열어요 (거버넌스 콘솔처럼 별도 셸인 화면용). */
  newTab?: boolean;
  /** 관리자 역할이 확인된 사용자에게만 보여요. */
  adminOnly?: boolean;
}

/*
 * 2026-09-06 (제품 오너 결정): 포털 상단 nav 에서 네 항목을 뺐어요 —
 * 「거버넌스」(`/governance`) · 「도구 인가 승인」 · 「도메인 정책」 · 「모니터링」.
 *
 * 라우트·컴포넌트·API 는 하나도 지우지 않았어요. 뒤의 세 개는 원래 `newTab: true` 로 관리자
 * 콘솔 화면을 새 창에 열던 항목이라, 콘솔(`/admin`)에서 계속 닿아요. 그래서 이건 **접근 제어가
 * 아니라 「포털 화면에서 안 보이게」** 한 거예요 — 실제 강제는 API 의 admin 게이트예요
 * (IH-163 결론).
 *
 * ⚠️ 「거버넌스 > 내 접근 권한」(`/governance`)만 성질이 달라요 — `adminOnly` 가 «아니었고»
 * 일반 사용자가 자기 권한을 보는 유일한 포털 진입점이었어요. 뺐으니 그 사용자는 URL 을 직접
 * 쳐야 해요. 위험대장 09-06 행에 적었어요.
 */
export const NAV: NavDomain[] = [
  {
    key: "catalog",
    label: "카탈로그",
    icon: "grid",
    status: "live",
    children: [
      { label: "둘러보기", href: "/catalog/browse" },
      { label: "등록", href: "/catalog/publish" },
      { label: "나의 요청", href: "/catalog/requests" },
      { label: "플러그인", href: "/catalog/bundles" },
    ],
  },
  {
    key: "playground",
    // 2026-09-06: 라벨만 「플레이그라운드」 → 「에이전트 관리」로 바꿨어요(제품 오너 결정).
    // `key`·`href` 는 그대로예요 — 경로를 바꾸면 북마크·심층링크가 깨지고, `key` 는
    // `nav.test.ts` 와 활성 메뉴 판정이 함께 쓰는 식별자예요.
    label: "에이전트 관리",
    icon: "play",
    // Agent Initializr가 동작하므로 live — 하위 메뉴가 항상 펼쳐져 진입점이 보여요.
    status: "live",
    children: [
      { label: "Agent Initializr", href: "/playground/initializr-strands" },
      { label: "Playground", href: "/playground" },
    ],
  },
  {
    // 자산 심사(거버넌스)와 Agent 인가를 함께 담는 독립 콘솔이라 "관리자 콘솔"로 올렸어요.
    // 설계: docs/design/admin-console-spec.md (§3.1).
    key: "admin",
    label: "관리자 콘솔",
    icon: "shield",
    status: "live",
    href: "/admin",
    newTab: true,
    adminOnly: true,
    children: [],
  },
];

export function visibleNav(isAdmin: boolean): NavDomain[] {
  return NAV.filter((domain) => !domain.adminOnly || isAdmin).map(
    (domain) => ({
      ...domain,
      children: domain.children.filter(
        (child) => !child.adminOnly || isAdmin,
      ),
    }),
  );
}

// 의존성 없는 인라인 SVG. (lucide-react 미설치라 직접 path 를 들고 다녀요.)
// 각 값은 <svg viewBox="0 0 24 24"> 안에 들어갈 자식 마크업이에요.
export const ICON_PATHS: Record<string, string> = {
  grid: '<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/>',
  server: '<rect x="3" y="4" width="18" height="6" rx="2"/><rect x="3" y="14" width="18" height="6" rx="2"/><path d="M7 7h.01M7 17h.01"/>',
  play: '<circle cx="12" cy="12" r="9"/><path d="M10 9l5 3-5 3z"/>',
  shield: '<path d="M12 2l8 4v6c0 5-3.5 8-8 10-4.5-2-8-5-8-10V6z"/>',
  check: '<path d="M9 11l3 3L22 4"/><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/>',
  search: '<circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/>',
  download: '<path d="M12 3v12"/><path d="m7 10 5 5 5-5"/><path d="M5 21h14"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  upload: '<path d="M12 16V4M7 9l5-5 5 5"/><path d="M5 20h14"/>',
  refresh: '<path d="M20 6v5h-5"/><path d="M4 18v-5h5"/><path d="M6.1 9a7 7 0 0 1 11.4-2.6L20 9M4 15l2.5 2.6A7 7 0 0 0 17.9 15"/>',
  // --- 알림(토스트·다이얼로그) 아이콘 ---
  alert: '<circle cx="12" cy="12" r="9"/><path d="M12 8v4M12 16h.01"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 16v-4M12 8h.01"/>',
  close: '<path d="M18 6 6 18M6 6l12 12"/>',
  // 도움말 트리거(help-popover.tsx). 원 + 물음표 — 다른 원형 아이콘(alert·info·play)과
  // 같은 r="9" 를 써서 같은 크기(size)로 나란히 놨을 때 지름이 어긋나지 않아요.
  help: '<circle cx="12" cy="12" r="9"/><path d="M9.3 9.2a2.8 2.8 0 0 1 5.4 1c0 1.9-2.7 2.4-2.7 3.9"/><path d="M12 17h.01"/>',
  // --- 관리자 콘솔 전용 아이콘 ---
  users: '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75"/>',
  list: '<path d="M4 6h16M4 12h16M4 18h10"/>',
  inbox: '<path d="M22 12h-6l-2 3h-4l-2-3H2"/><path d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/>',
  chart: '<path d="M3 3v18h18"/><rect x="7" y="10" width="3" height="7"/><rect x="12" y="6" width="3" height="11"/><rect x="17" y="13" width="3" height="4"/>',
  wrench: '<path d="M14.7 6.3a4 4 0 0 0-5.4 5.4L3 18v3h3l6.3-6.3a4 4 0 0 0 5.4-5.4l-2.6 2.6-2-2 2.6-2.6z"/>',
  scroll: '<path d="M8 3H5a2 2 0 0 0-2 2v3M8 3h11a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H8M8 3v18M3 8h5m-5 4h5m-5 4h5"/>',
  settings: '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/>',
  menu: '<path d="M3 12h18M3 6h18M3 18h18"/>',
  back: '<path d="M19 12H5M12 19l-7-7 7-7"/>',
  external: '<path d="M15 3h6v6"/><path d="M10 14 21 3"/><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/>',
  link: '<path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/>',
  eye: '<path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>',
  eyeOff: '<path d="m3 3 18 18"/><path d="M10.6 10.6a2 2 0 0 0 2.8 2.8M9.9 5.2A10.8 10.8 0 0 1 12 5c6.5 0 10 7 10 7a17.7 17.7 0 0 1-2.1 3.2M6.6 6.6C3.6 8.5 2 12 2 12s3.5 7 10 7a10.7 10.7 0 0 0 5.4-1.5"/>',
};
