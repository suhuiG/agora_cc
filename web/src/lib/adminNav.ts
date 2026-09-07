// 관리자 콘솔 내비게이션 정의 (단일 출처).
// admin 전용 URL(/admin/*)에서만 쓰는 사이드바예요.
// 일반 포탈 NAV(nav.ts)와 분리 — Reviewer/Admin 전용 화면이라 URL·레이아웃을 별도로 둬요.
//
// 화면 설계 출처: docs/design/admin-console-spec.md (§3.2).
// 섹션 3개로 나눠요 — 자산 심사(거버넌스)와 Agent 인가는 판단 대상·시점·데이터가 달라서
// 한 평면에 섞으면 인가 화면이 "심사 도구"로 읽혀요.
//  - 사용자 · 인가 : 누가 무엇을 호출해도 되나 (agora-identity 테이블)
//  - 거버넌스      : 자산이 안전한가 (Registry + 스캔 결과)
//  - 운영          : 모니터링·호출 이력·카테고리·저장소 연결·설정
//
// 화면별 역할:
//  - 승인 큐        : 검토 대기 자산 + 진행상태 + 검출 위협 (자산명 클릭 → 상세)
//  - 대시보드       : 통과율·위험분포·등급분포·SLA 관측
//  - 자산 인벤토리  : 소유·상태·중복 관점의 전 자산 현황 (측정 축은 telemetry 이후)
//  - 카테고리       : 등록 폼 카테고리 마스터 목록 관리
//  - 거버넌스 도구  : 오픈소스 스캐너 도구 등록·수정·버전관리 + 등급별 구성
//  - 심사 로그      : 판정 이력 (누가·언제·무엇을·오버라이드 사유)

export interface AdminNavItem {
  label: string;
  href: string;
  /** nav.ts ICON_PATHS 키. */
  icon: string;
  /** 현재 생산자는 0개예요. 렌더 계약 정리는 별 범위라 타입은 유지해요. */
  pending?: boolean;
}

export interface AdminNavSection {
  key: string;
  label: string;
  items: AdminNavItem[];
}

export const ADMIN_BASE = "/admin";

/** 승인 큐 경로. 예전엔 base 가 승인 큐를 겸했지만 지금은 base 가 콘솔 홈이에요. */
export const ADMIN_QUEUE = `${ADMIN_BASE}/queue`;

export const ADMIN_NAV: AdminNavSection[] = [
  {
    key: "identity",
    label: "사용자 · 인가",
    items: [
      { label: "사용자 관리", href: `${ADMIN_BASE}/users`, icon: "users" },
      // 도구 인가 승인 (ADR-0099) — 신청 하나의 인가 두 층 ④⑦ 을 한 화면에서 닫아요.
      // "Agent × Tool" 은 agent 축으로 ④층을 관리하는 화면이고, 이 화면은 신청 축으로 두 층을
      // 엮어요. 둘을 합치지 않은 이유는 컴포넌트 주석에 있어요
      // (components/admin/authorization/ToolAuthorizationClient.tsx).
      // ⚠️ 그 "Agent × Tool" 은 2026-09-06 에 nav 에서 숨겼어요 — 아래 「nav 에서 뺀 항목」 참고.
      // 라우트는 살아 있으니 이 주석의 설명은 그대로 유효해요.
      {
        label: "도구 인가 승인",
        href: `${ADMIN_BASE}/tool-authorization`,
        icon: "inbox",
      },
      // 도구 호출 주체 (ADR-0099 결정 7, IH-145) — ⑦층을 **도구 축**으로 CRUD 해요.
      // 위의 "도구 인가 승인" 과 축이 달라요: 저쪽은 「이 agent 의 신청을 승인할까」,
      // 이쪽은 「이 도구를 부를 자격이 있는 사람·그룹이 누구인가」예요. ⑦은 agent 와 무관해서
      // agent 화면 아래에 두면 안 돼요.
      // 경로가 서로의 접두어가 아니어야 해요(사이드바 활성 판정이 최장 prefix 예요) —
      // `/admin/tool-access` 와 `/admin/tool-authorization` 은 서로 접두어가 아니에요.
      {
        label: "도구 호출 주체",
        href: `${ADMIN_BASE}/tool-access`,
        icon: "users",
      },
      // (숨김) "Agent × Tool"·"인가 원장" 이 여기 있었어요 — 아래 「nav 에서 뺀 항목」 참고.
      //
      // 숨긴 "인가 원장" 은 원장이 아는 agent 를 기준으로 대조해요. 이 "Gateway 정책" 화면은
      // 반대로 Gateway 를 출발점으로 잡아 **원장 밖 정책**까지 봐요 — 옛 PoC·컷오버 잔재가
      // 거기 숨어요. 그래서 원장 축 두 화면을 숨겨도 라이브 전수 감사 경로는 남아요.
      {
        label: "Gateway 정책",
        href: `${ADMIN_BASE}/gateway-policies`,
        icon: "server",
      },
      // (숨김) "Cedar 정책"(`/admin/policies`) 이 여기 있었어요 — 아래 목록 참고.
      // 도메인 정책 (IH-132) — 요청 인자 값에 걸는 Cedar 규칙이에요. "Gateway 정책" 은 라이브
      // Cedar 전부를 훑는 감사 화면이고, 이 화면은 규칙 하나를 만드는 작업 화면이에요.
      // 경로가 서로의 접두어가 아니어야 해요(AdminSidebar 의 활성 판정이 최장 prefix 예요).
      {
        label: "도메인 정책",
        href: `${ADMIN_BASE}/domain-policies`,
        icon: "scroll",
      },
      // "Tool binding 승인" 메뉴 제거(IA-25, ADR-0020) — agent-tool 인가를 admin이 매트릭스에서
      // 직접 ALLOWED로 부여하므로 REQUESTED 승인 큐가 불필요. 라우트·컴포넌트는 롤백 위해 존치.
      //
      // ── nav 에서 뺀 항목 (지금 5개) ────────────────────────────────────────────────
      //
      // **「사용자 권한」(`/admin/grants`)** — 「사용자 관리」 화면이 그 기능을 **흡수했어요**
      //   (제품 오너 결정, 2026-09-06: 「사용자 권한 메뉴에서 사용자별 상세 권한을 사용자 관리
      //   메뉴에서 사용자 선택시 하단에 나오게. 즉 2개 메뉴 UI 합치기」).
      //
      //   ⚠️ **이유를 정확히 써요 — 「안 쓰는 메뉴라서」가 아니에요.** 2026-09-06 PR #219 적대적
      //   리뷰가 그 판정을 반박했고 그게 맞았어요: 그 화면은 «현행» ⑦ grant 를 전역으로 조회하고
      //   `revoke_access_grant` 로 실제 회수하는 살아 있는 관리 기능이에요. 지금 빼는 것이 정당한
      //   조건은 딱 하나였고, 그 조건이 이제 성립해요 — **그 조회·회수가 도달 가능한 다른 화면으로
      //   실제로 옮겨졌어요**:
      //     · `components/admin/users/UserGrantsPanel.tsx` 가 `listAccessGrants` 로 전역 목록을
      //       받아 사람 축 + 그룹 축(판정의 합집합)으로 갈라 그리고, `deleteAccessGrant` 로
      //       회수해요. 회수 가능 판정·확인 문구는 `adminGrants` 의 그 함수를 그대로 써요.
      //     · `components/admin/users/UsersClient.tsx` 가 사용자를 고르면 그 패널을 그려요.
      //   **그 흡수를 `web/src/lib/nav.test.ts` 의 다른 단정이 지켜요** — 두 파일의 소스 텍스트를
      //   읽어 조회·회수 배선을 단정하니, 패널을 지우고 이 항목만 빼면 테스트가 빨개져요.
      //
      //   라우트·페이지·컴포넌트는 남겨요 — 옛 행 필터처럼 통합 패널이 흡수하지 않은 축이 아직
      //   거기 있어요. 그리고 새 패널은 사용자를 골라야 보이니, **어느 사용자에도 붙지 않는 행**
      //   (주체 미기록·회원 0명 그룹)은 패널 하단에서 세어 밝혀요(`grantReachability`).
      //
      // **「권한 그룹 정의 (인가 미사용)」(`/admin/capability-sets`)** — ADR-0112 가 「폐기된
      // 층의 화면을 읽기 전용으로 보존」하기로 했고, 라우트·컴포넌트·API 를 남기는 것과 nav
      // 에서 빼는 것은 다른 축이에요(IH-163 선례가 그 형태예요). 화면 상단 배너가 이미
      // 「여기서 무엇을 바꿔도 도구 인가는 달라지지 않아요」라고 말해요.
      //
      // 아래 3개는 2026-09-06 제품 오너 결정(「메뉴 숨기기 : Agentxtool, 인가원장, Cedar정책」,
      // 09-07 시연 준비)로 뺐어요. 한 줄씩 이유예요:
      //
      // **「Agent × Tool」(`/admin/agents`)** — ④층을 agent 축으로 켜고 끄는 화면인데, 시연
      //   경로는 신청 축인 「도구 인가 승인」(`/admin/tool-authorization`)이 같은 ④층을 닫아서
      //   사이드바에 둘을 나란히 두면 「어디서 승인하나」가 갈려요.
      //   ⚠️ 다만 이 화면은 ④ binding 을 **직접 REVOKED 로 되돌리는 유일한 화면**이에요
      //   (`AgentToolMatrix.tsx` 의 `act("revoke")`). 그래서 이건 「기능이 없는 메뉴」가 아니라
      //   **「시연 화면에서 안 보이게」** 한 것이고, 회수는 URL 로 직접 들어가서 해요. 화면 안
      //   안내문(`authorizationChain.ts` 의 `ACTION_LABELS`)이 아직 「Agent × Tool 화면에서」라고
      //   말하니, 그 문구를 신뢰하는 관리자는 메뉴를 못 찾아요 — 시연 후 되살릴 자리예요.
      // **「인가 원장」(`/admin/authorization-inventory`)** — 원장↔live Cedar 대조 **감사** 화면
      //   이에요. 정리(삭제) 요청은 없어요 — 후보는 `report_only` 이고 DELETE 경로 자체가 없다는
      //   걸 `authorizationInventory.test.ts` 가 단정해요.
      //   ⚠️ **그런데 「쓰기가 없다」는 거짓이에요.** 이 화면은 fail-closed 인가 게이트를 승인하는
      //   `POST /api/admin/identity/fail-closed-authorization-gate/approve` 의 **유일한 UI 경로**
      //   예요(`api/authorizationInventory.ts` 의 `approveAuthorizationInventoryGate`). 첫 판 주석이
      //   「DELETE 가 없다」는 단정에서 「쓰기가 없다」를 도출했는데, 그건 다른 사실이에요
      //   (2026-09-06 적대적 검증이 잡았어요). 그래서 이 메뉴를 숨기면 **그 승인이 사이드바에서
      //   도달 불가**해져요 — URL 로 직접 들어가야 해요.
      // **「Cedar 정책」(`/admin/policies`)** — agent 축 정책 인벤토리인데 라이브에는 공유 정책
      //   한 장만 있어서(ADR-0093·ADR-0099) agent 별로 볼 게 없어요. 이 화면도 읽기 전용이고
      //   (새로고침·재시도 버튼뿐), 라이브 전수 감사는 위 「Gateway 정책」이 담당해요.
      //
      // ⚠️ **메뉴를 뺀 것은 접근 제어가 아니에요.** 다섯 라우트·페이지·컴포넌트·API 는 그대로
      // 남아 있고 URL 로 직접 열려요. `AdminGuard` 는 클라이언트 체크이고 실제 강제는 API 의
      // admin 게이트예요(IH-163 결론 · ADR-0111 결정 1). `web/src/proxy.ts` 도 일부러
      // 건드리지 않았어요.
      //
      // ⚠️ nav 에서 뺀 것과 **화면에서 사라진 것**도 달라요. 콘솔 홈(`ConsoleHomeClient.tsx`)의
      // 「지금 봐야 할 것」 카드 하나가 `/admin/agents` 를 하드코딩해서 링크해요 — ADMIN_NAV 를
      // 안 읽는 자리라 이 파일만 고쳐서는 안 없어져요.
    ],
  },
  {
    key: "governance",
    label: "거버넌스",
    items: [
      { label: "승인 큐", href: `${ADMIN_QUEUE}`, icon: "inbox" },
      { label: "대시보드", href: `${ADMIN_BASE}/dashboard`, icon: "chart" },
      { label: "자산 인벤토리", href: `${ADMIN_BASE}/inventory`, icon: "grid" },
      { label: "거버넌스 도구", href: `${ADMIN_BASE}/tools`, icon: "wrench" },
      // 기존 audit 은 거버넌스 "심사" 이력이에요. 신규 audit-calls(tool 호출 이력)와
      // 라벨을 갈라 혼동을 막아요.
      { label: "심사 로그", href: `${ADMIN_BASE}/audit`, icon: "scroll" },
      { label: "플러그인", href: `${ADMIN_BASE}/bundles`, icon: "grid" },
    ],
  },
  {
    key: "ops",
    label: "운영",
    items: [
      {
        label: "모니터링",
        href: `${ADMIN_BASE}/agents/monitoring`,
        icon: "chart",
      },
      {
        label: "호출 감사 로그",
        href: `${ADMIN_BASE}/audit-calls`,
        icon: "scroll",
      },
      { label: "카테고리", href: `${ADMIN_BASE}/categories`, icon: "settings" },
      { label: "저장소 연결", href: `${ADMIN_BASE}/connection`, icon: "external" },
      { label: "설정", href: `${ADMIN_BASE}/settings`, icon: "settings" },
    ],
  },
];
