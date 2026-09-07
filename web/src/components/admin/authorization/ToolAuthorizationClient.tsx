"use client";

import { Fragment, useCallback, useEffect, useMemo, useState } from "react";
import useSWR from "swr";
import {
  approveAuthorizationChain,
  diagnoseAuthorizationRequest,
  listAuthorizationRequests,
  listCognitoUsers,
  provisionChainAccessGrant,
  isPlatformGrantGroup,
  PLATFORM_GRANT_GROUPS,
  type AuthorizationRequestDiagnosis,
  type AuthorizationRequestPage,
  type AuthorizationRequestRow,
  type ChainApprovalResult,
  type ChainLayerStatus,
  type ChainTarget,
  type GrantSubject,
  type PlatformGrantGroup,
} from "@/lib/api";
import {
  ACTION_LABELS,
  AUTHORIZATION_COLUMNS,
  CHAIN_LAYERS,
  DEFAULT_SORT,
  agentColumnLabel,
  agentColumnNote,
  agentLabel,
  assetStateBadges,
  assetLabel,
  blastRadiusMessage,
  chainApprovalMessage,
  chainApprovalTone,
  defaultGrantPrincipal,
  isActionable,
  layerMessage,
  layerTone,
  liftObservationForRequester,
  nextAction,
  progressSteps,
  requesterLabel,
  sortRows,
  stepFor,
  toggleSort,
  type ChainAction,
  type ChainApprovalTone,
  type AssetStateBadgeTone,
  type LayerTone,
  type SortKey,
  type SortState,
} from "@/lib/authorizationChain";
import { toolGrantGroupLabel, toolLabel } from "@/lib/toolAccess";
import { formatKst } from "@/lib/auditCalls";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { HelpPopover } from "@/components/ui/help-popover";
import { Icon } from "@/components/ui/icon";
import { SensitivityBadge } from "@/components/SensitivityBadge";
import { SensitivityDisclosure } from "@/components/admin/SensitivityDisclosure";
import { PrincipalPicker } from "./PrincipalPicker";
import {
  EmptyState,
  InlineError,
  LoadError,
  LoadingRows,
  PageHeading,
  errorMessage,
} from "@/components/admin/access/shared";
import { cn } from "@/lib/ui";

// 도구 인가 승인 (/admin/tool-authorization) — ADR-0099(층 2개)·0097·0098.
//
// **기존 «Tool binding 승인 큐»(/admin/agents/approvals)를 확장한 게 아니에요.** 그 화면은
// ④층 하나만 다루고, 이 화면은 두 층을 엮어요. 기존 큐는 그대로 남아 있어요.
//
// 축이 다른 화면이 하나 더 있어요 — `/admin/tool-access` 는 **도구** 축으로 ⑦층을 CRUD 해요
// (「이 도구를 부를 수 있는 사람·그룹」). 이 화면은 **신청** 축이에요(「이 agent 의 이 신청을
// 승인할까」). 합치지 않아요 — ADR-0099 결정 7 의 표.
//
// 화면 설계는 `docs/research/2026-08-31-ih127-tool-authorization-ui-options.html` 의 확정안
// (안 B + 보완 6건, 2026-08-31 제품 오너 결정)이에요:
//   ① 표 + 헤더 정렬  ② 신청 사유 여러 줄  ③ 신청자 이름·이메일 분리
//   ④ 진행선에 단계명(사슬 번호 제거)  ⑤ 사람 권한은 표 폭에 맞춘 펼침 행
//   ⑥ 사람/그룹 부여  ⑦ 설명은 `?` 팝오버
//
// **목록은 ④칸만 확정해요.** ⑦층은 소비자 경로 Query 가 필요해서 행을 펼칠 때 단건으로
// 확정해요 — 목록에서 초록불로 위장하지 않아요(ADR-0037 §4).
//
// 자산·agent·신청자는 **id 가 아니라 값**으로 보여줘요. record_id 는 `fAUPJWslfkyZ` 처럼
// 사람이 구분할 수 없는 값이라, 그것만 보여주면 관리자가 무엇을 승인하는지 알 수 없어요.

const CACHE_KEY = "admin/authorization-requests";

type Feedback = { tone: ChainApprovalTone; message: string };

export function ToolAuthorizationClient() {
  const [include, setInclude] = useState<"actionable" | "all">("actionable");
  const [sort, setSort] = useState<SortState>(DEFAULT_SORT);
  const [openRow, setOpenRow] = useState("");
  // 행별 exact-key 진단 결과. **목록 payload 가 아니라 여기가 ⑦칸 색의 근거예요.**
  //
  // 펼침 패널이 이미 부르는 `diagnoseAuthorizationRequest` 응답을 그대로 올려 담아요 — 새
  // 엔드포인트도, `listToolGrants` scan 도 쓰지 않아요(둘 다 IH-161 이 금지해요). 관측한 행만
  // 색이 바뀌니 「목록에 그렇게 적혀 있다」가 초록의 근거가 되는 일은 없어요.
  //
  // 한 번 관측한 결과는 행을 접어도 남겨요. 접을 때 지우면 승인 직후 초록이던 점이 접자마자
  // 회색으로 돌아가서, 관리자가 「부여가 안 됐나」로 읽어요. 대신 그 값은 **관측 시점의**
  // 사실이라 다시 펼치면 SWR 이 재검증해요.
  //
  // ⚠️ **관측은 «주체별» 이라 신청자 기준일 때만 목록 점에 올려요.** 서버는 ⑦ 를
  // `principal_id or binding.created_by` 로 판정해요. 관리자가 부여 대상을 그룹이나 다른
  // 사람으로 바꿔 관측한 초록을 그대로 올리면, 목록이 「신청자가 부를 수 있다」로 읽히는데
  // 신청자는 아직 못 불러요 — IH-127 계열(초록 배지인데 호출은 거부)이에요. 그래서 주체가
  // 신청자가 아니면 **회색을 유지**해요. 회색은 통과도 거부도 아니고 「이 주체로는 아직 확인
  // 안 했다」예요.
  const [observed, setObserved] = useState<Record<string, ChainLayerStatus[]>>({});
  const observe = useCallback(
    (key: string, steps: ChainLayerStatus[], subject: string, expected: string) => {
      // 판정은 `authorizationChain.liftObservationForRequester` 가 소유해요 — `.tsx` 는 테스트
      // 러너가 돌리지 않아서(`package.json` `test` 에 `.tsx` 0건) 여기 두면 음성 대조를 걸 수 없어요.
      if (!liftObservationForRequester(subject, expected)) return;
      setObserved((current) =>
        current[key] === steps ? current : { ...current, [key]: steps },
      );
    },
    [],
  );
  const { data, error, isLoading, mutate } = useSWR<AuthorizationRequestPage>(
    `${CACHE_KEY}?${include}`,
    () => listAuthorizationRequests(include),
  );
  // 「다시 조회」는 목록만 갱신하면 안 돼요 — 다른 화면(`/admin/tool-access`)에서 ⑦ 가 회수되면
  // 이 맵의 옛 초록이 남아 「부를 수 있다」고 계속 말해요.
  //
  // ⚠️ **`setObserved({})` 만으로는 안 돼요** (2026-09-06 PR #219 적대적 리뷰가 재현했어요).
  // 행을 «펼친 채» 누르면 그 패널의 진단 SWR 캐시가 그대로 남아 있고, 부모가 재렌더될 때
  // 인라인 `onObserved` 참조가 바뀌어 자식 effect 가 **옛 `steps` 를 즉시 다시 올려요** —
  // 비운 맵이 한 프레임 만에 stale 초록으로 되돌아와요. 그래서 셋을 함께 해요:
  //   ⑴ 관측 맵 비우기 ⑵ **펼친 행 닫기**(패널 unmount → 재주입 경로 자체를 없애요)
  //   ⑶ 진단 SWR 캐시를 `undefined` 로 지우기(다시 펼쳤을 때 올릴 옛 값이 없어야 해요)
  //
  // ⚠️ 첫 처방(SWR 캐시를 `undefined` 로 지우기)은 **다른 결함을 만들었어요** — 비운 키를
  // 패널이 remount 할 때 SWR 이 mount 재검증을 건너뛰어서, 「다시 조회」 뒤 그 행을 다시
  // 펼쳐도 진단이 안 나가고 회색에 머물렀어요(2026-09-06 브라우저 실측: 25초 대기에도
  // diagnose GET 1건 그대로. 접었다 펼치면 그때 나갔어요).
  //
  // 그래서 캐시를 «지우는» 대신 **키를 바꿔요.** 세대 토큰을 진단 SWR 키에 넣으면
  //   ⑴ 새 키에는 캐시가 없으니 옛 값을 올릴 수가 «없고»(stale 재주입 경로가 구조적으로 사라져요)
  //   ⑵ mount 재검증 의미론에 의존하지 않고 «반드시» 새로 조회해요
  // 펼친 행을 닫지 않아도 돼요 — 그 행은 새 키로 다시 확인해서 스스로 옳은 색이 돼요.
  const [refreshToken, setRefreshToken] = useState(0);
  const refresh = useCallback(() => {
    setObserved({});
    setRefreshToken((current) => current + 1);
    void mutate();
  }, [mutate]);
  // 정렬은 SWR 데이터를 제자리에서 바꾸지 않아요 — `sortRows` 가 복사해요. 낙관적 `mutate`
  // 가 배열을 다시 쓰는 동안 정렬이 재계산되어야 해요(QueueClient.tsx:59-82 와 같은 이유).
  const rows = useMemo(() => sortRows(data?.requests ?? [], sort), [data?.requests, sort]);

  return (
    <div className="min-w-0">
      {/* `PageHeading.title` 은 `string` 이에요 (`admin/access/shared.tsx:37` — description 만
          ReactNode). 그래서 `?` 팝오버는 description 쪽에 둬요. */}
      <PageHeading
        title="도구 인가 승인"
        description={
          <span className="inline-flex flex-wrap items-center gap-1.5">
            Initializr 화면에서 신청한 도구의 권한을 사용자와 Agent 에게 부여해요.
            <HelpPopover label="이 화면이 하는 일">
              <p>
                호출이 되려면 두 가지가 갖춰져야 해요 — <strong>agent 가 이 도구를 부를 수
                있게 승인됐나</strong>(binding), 그리고 <strong>부르는 사람이 이 도구를 부를
                자격이 있나</strong>(사람 권한). 신청은 그중 앞의 하나만 만들어요.
              </p>
              <p className="mt-2">
                「승인」을 누르면 둘이 한 번에 채워져요. 관리자가 정하는 건 «누구에게 이 도구를
                열지» 하나예요.
              </p>
            </HelpPopover>
          </span>
        }
      />

      <div className="mb-4 flex flex-wrap items-center gap-2">
        {(["actionable", "all"] as const).map((value) => (
          <Button
            key={value}
            size="sm"
            variant={include === value ? "primary" : "outline"}
            onClick={() => setInclude(value)}
          >
            {value === "actionable" ? "할 일만" : "전체"}
          </Button>
        ))}
        <Button size="sm" variant="outline" onClick={refresh}>
          <Icon name="refresh" size={14} />
          다시 조회
        </Button>
        {data && (
          <span className="ml-auto inline-flex items-center gap-1 text-xs text-muted-foreground">
            {data.counts.totalBindings}건 중 {data.counts.returned}건
            {data.counts.skippedReadyRead > 0 && (
              <>
                {" · "}④가 충족된 READ 신청 {data.counts.skippedReadyRead}건 숨김
                <HelpPopover label="숨긴 건수 설명">
                  ⑦는 이 목록에서 확인하지 않아요. «전체» 를 누른 뒤 행을 펼치면 사람·그룹
                  권한을 정확한 키로 확인해요.
                </HelpPopover>
              </>
            )}
          </span>
        )}
      </div>

      {data && !data.directoryObserved && (
        <div
          role="alert"
          className="mb-4 rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-sm text-amber-900"
        >
          사용자 목록을 읽지 못해 신청자 이름·이메일이 비어 있어요. 「사람이 아닌 신청자」와
          구분할 수 없는 상태라, 부여 대상은 직접 골라 주세요.
        </div>
      )}
      {data?.counts.truncated && (
        <div
          role="alert"
          className="mb-4 rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-sm text-amber-900"
        >
          신청이 {data.counts.limit}개를 넘어 일부만 표시했어요 (원장 전체{" "}
          {data.counts.totalBindings}건). 처리한 뒤 다시 조회해 주세요.
        </div>
      )}

      {isLoading ? (
        <LoadingRows label="인가 신청 불러오는 중" />
      ) : error ? (
        <LoadError message="인가 신청을 불러오지 못했어요." />
      ) : rows.length === 0 ? (
        <EmptyState
          title={
            include === "actionable"
              ? "처리할 인가 신청이 없어요."
              : "인가 신청이 없어요."
          }
          description={
            include === "actionable"
              ? "아직 호출할 수 없는 신청과 쓰기 도구 신청이 여기 표시돼요."
              : "Agent 가 MCP operation 권한을 신청하면 여기 표시돼요."
          }
        />
      ) : (
        // 표가 넓어서 가로 스크롤을 둬요. `?` 팝오버는 portal 로 띄워서 여기 잘리지 않아요.
        <div className="overflow-x-auto rounded-lg border border-border">
          {/* «대상 agent» 칸이 늘어난 만큼 최소 폭도 늘려요 — 안 늘리면 신청 사유 칸이 먼저
              찌그러져요. */}
          <table className="w-full min-w-[1320px] border-collapse text-left">
            <thead>
              <tr className="border-b border-border bg-muted/40">
                {AUTHORIZATION_COLUMNS.map((column) => (
                  <SortableTh
                    key={column.label || "actions"}
                    column={column}
                    sort={sort}
                    onSort={(key) => setSort((current) => toggleSort(current, key))}
                  />
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => {
                const key = rowKey(row);
                return (
                  <Fragment key={key}>
                    <RequestRow
                      row={row}
                      observedSteps={observed[key]}
                      open={openRow === key}
                      onToggle={() => setOpenRow(openRow === key ? "" : key)}
                    />
                    {openRow === key && (
                      // 표 폭에 맞춘 펼침 행 — 가운데 모달을 쓰지 않아요. 선례는
                      // `AgentPolicyInventoryClient.tsx:466-478` 의 두 번째 `<tr colSpan>`.
                      <tr className="border-b border-border bg-[#f8fbff]">
                        <td
                          colSpan={AUTHORIZATION_COLUMNS.length}
                          className="px-3 py-3"
                        >
                          <GrantPanel
                            row={row}
                            onChanged={() => mutate()}
                            refreshToken={refreshToken}
                            onObserved={(steps, subject) =>
                              // 기대 주체 = 신청자. 다른 주체로 관측한 초록은 목록에
                              // 올리지 않아요 (위 `observe` 주석: IH-127 계열).
                              observe(
                                key,
                                steps,
                                subject,
                                defaultGrantPrincipal(row).principalId,
                              )
                            }
                          />
                        </td>
                      </tr>
                    )}
                  </Fragment>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function rowKey(row: AuthorizationRequestRow): string {
  return `${row.agentId}:${row.assetId}:${row.assetVersion}:${row.operationId}`;
}

function SortableTh({
  column,
  sort,
  onSort,
}: {
  column: { key: SortKey | null; label: string };
  sort: SortState;
  onSort: (key: SortKey) => void;
}) {
  const active = column.key !== null && sort.key === column.key;
  // `aria-sort` 는 이 저장소에 선례가 없어요(0건). 관례를 따르는 게 아니라 개선이라
  // 의도적으로 넣어요 — 스크린리더가 정렬 상태를 읽을 방법이 이것뿐이에요.
  const ariaSort = !active ? "none" : sort.dir === "asc" ? "ascending" : "descending";
  return (
    <th
      scope="col"
      aria-sort={column.key === null ? undefined : ariaSort}
      className="whitespace-nowrap px-3 py-2 text-[11.5px] font-semibold text-muted-foreground"
    >
      {column.key === null ? (
        column.label
      ) : (
        <button
          type="button"
          onClick={() => onSort(column.key!)}
          className={cn(
            "inline-flex items-center gap-1 rounded focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
            active && "text-primary",
          )}
        >
          {column.label}
          <span aria-hidden className="text-[10px]">
            {active ? (sort.dir === "asc" ? "▲" : "▼") : "↕"}
          </span>
        </button>
      )}
      {column.label === "진행" && (
        <HelpPopover label="진행 칸 설명">
          호출이 되려면 두 칸이 다 채워져야 해요. 첫 칸(binding)은 「승인」이 채우고, 둘째 칸
          «사람 권한» 은 누구에게 열지 정해요. 둘째 칸은 아직 확인하지 않았으면 회색이에요 —
          확인에 사람별 조회가 필요해서 행을 펼칠 때 정확한 키로 확인해요. 회색은 통과도
          거부도 아니에요. <strong>행을 펼쳐 확인한 뒤에는 그 관측 결과대로 색이 바뀌어요</strong>
          — 승인하고 권한을 부여하면 그 자리에서 초록이 돼요.
        </HelpPopover>
      )}
    </th>
  );
}

const TONE_CLASS: Record<LayerTone, string> = {
  ok: "bg-emerald-500 border-emerald-500",
  todo: "bg-white border-amber-500",
  blocked: "bg-red-500 border-red-500",
  // 관측하지 못한 상태는 초록도 빨강도 아니에요 (ADR-0037 §4).
  unknown: "bg-slate-300 border-slate-300",
};

const TONE_LABEL: Record<LayerTone, string> = {
  ok: "충족",
  todo: "필요",
  blocked: "막힘",
  unknown: "확인 필요",
};

const ASSET_BADGE_CLASS: Record<AssetStateBadgeTone, string> = {
  neutral: "bg-slate-100 text-slate-700",
  ok: "bg-emerald-100 text-emerald-700",
  warn: "bg-amber-100 text-amber-800",
  unknown: "bg-slate-200 text-slate-700",
};

function RequestRow({
  row,
  observedSteps,
  open,
  onToggle,
}: {
  row: AuthorizationRequestRow;
  /** 이 행의 exact-key 진단 `steps`. 아직 관측하지 않았으면 `undefined` 예요. */
  observedSteps?: ChainLayerStatus[];
  open: boolean;
  onToggle: () => void;
}) {
  const action = nextAction(row);
  const assetBadges = assetStateBadges(row);
  const assetObserved = row.assetObservation === "observed";
  const assetFound = assetObserved && row.assetFound === true;
  return (
    <tr className={cn("border-b border-border align-top", open && "bg-[#f5f9ff]")}>
      {/* 대상 agent — 목록의 첫 질문이 「어느 agent 가」예요. 이름은 Registry, id 는 원장에서
          와요. 이름을 못 읽으면 id 를 그리고 그 사실을 밝혀요(부재로 단정하지 않아요). */}
      <td className="px-3 py-2.5 text-[12.5px]">
        <div className="font-medium" title={row.agentId}>
          {agentColumnLabel(row)}
        </div>
        {agentColumnNote(row) && (
          <div className="text-[11px] text-muted-foreground">{agentColumnNote(row)}</div>
        )}
      </td>
      <td className="px-3 py-2.5 text-[12.5px]">
        <div className="font-medium">{assetFound ? row.assetName : row.assetId}</div>
        <div className="text-[11px] text-muted-foreground">
          {!assetObserved
            ? "Registry를 관측하지 못했어요"
            : assetFound
              ? `v${row.assetVersion}`
              : "Registry 에 없어요"}
        </div>
        <div className="mt-1.5 flex max-w-[180px] flex-wrap gap-1">
          {assetBadges.map((badge) => (
            <Badge
              key={badge.axis}
              variant="type"
              className={cn("text-[10px]", ASSET_BADGE_CLASS[badge.tone])}
              title={badge.detail}
            >
              {badge.label}
            </Badge>
          ))}
        </div>
      </td>
      <td className="px-3 py-2.5">
        <div className="font-mono text-[12px] font-semibold">{row.operationId}</div>
        <div className="mt-1">
          <SensitivityBadge sensitivity={row.sensitivity} />
        </div>
      </td>
      {/* 신청 사유는 길어질 수 있어서 여러 줄로 보여줘요. 셀이 `align-top` 이어야 다른
          칼럼이 아래로 딸려가지 않아요. */}
      <td className="min-w-[210px] max-w-[320px] whitespace-pre-wrap px-3 py-2.5 text-[12px] leading-relaxed">
        {row.requestJustification || <span className="text-muted-foreground">—</span>}
      </td>
      <td className="px-3 py-2.5 text-[12.5px]">
        {row.requestedByName || (
          <span className="text-muted-foreground">
            {row.requesterObservation === "NOT_IN_DIRECTORY" ? "시스템 생성" : "—"}
          </span>
        )}
      </td>
      <td className="px-3 py-2.5 text-[11.5px] text-muted-foreground">
        {row.requestedByEmail || "—"}
      </td>
      <td className="whitespace-nowrap px-3 py-2.5 text-[11.5px] text-muted-foreground">
        {/* `""` 는 «없음» 이 아니라 이 기능 이전 신청이에요 — 빈칸으로 그려요. */}
        {row.requestedAt ? formatKst(row.requestedAt) : "—"}
      </td>
      <td className="whitespace-nowrap px-3 py-2.5 text-[11.5px] text-muted-foreground">
        {row.approvedAt ? formatKst(row.approvedAt) : "—"}
      </td>
      <td className="px-3 py-2.5">
        <ProgressChain steps={progressSteps(row, observedSteps)} />
      </td>
      <td className="whitespace-nowrap px-3 py-2.5 text-right">
        <Button size="sm" variant={open ? "primary" : "outline"} onClick={onToggle}>
          {open ? "접기" : isActionable(action) ? ACTION_LABELS[action] : "자세히"}
        </Button>
      </td>
    </tr>
  );
}

/**
 * 진행선 — 색의 근거는 넘겨받은 `steps` 예요.
 *
 * 예전에는 `row.steps`(목록 payload)만 봤어요. 그 payload 의 ⑦칸은 서버가 일부러 `UNKNOWN`
 * 으로 주니까 **사람 권한을 부여한 뒤에도 영원히 회색**이었어요. 이제 `progressSteps` 가 그
 * 행의 exact-key 진단이 도착했을 때만 그 값으로 바꿔 줘요 — 목록 payload 로는 초록이 되지
 * 않아요(IH-127 재발 방지).
 */
function ProgressChain({ steps }: { steps: ChainLayerStatus[] }) {
  return (
    <div className="flex flex-wrap items-center gap-0">
      {CHAIN_LAYERS.map((definition, index) => {
        const status = stepFor(steps, definition.layer);
        const tone: LayerTone = status ? layerTone(status.state) : "unknown";
        return (
          <Fragment key={definition.layer}>
            {index > 0 && (
              <span
                aria-hidden
                className={cn(
                  "mx-1.5 h-0.5 w-3.5",
                  tone === "ok" ? "bg-emerald-500" : "bg-slate-300",
                )}
              />
            )}
            <span className="inline-flex items-center gap-1">
              <span
                aria-hidden
                className={cn("h-2.5 w-2.5 shrink-0 rounded-full border-2", TONE_CLASS[tone])}
              />
              <span
                className={cn(
                  "whitespace-nowrap text-[11px]",
                  tone === "ok" ? "text-muted-foreground" : "font-semibold text-amber-800",
                  tone === "blocked" && "font-semibold text-red-700",
                  tone === "unknown" && "font-normal text-muted-foreground",
                )}
                title={`${definition.title}: ${TONE_LABEL[tone]}`}
              >
                {definition.title}
              </span>
            </span>
          </Fragment>
        );
      })}
    </div>
  );
}

/**
 * 펼침 패널 — 표 폭에 맞춰요.
 *
 * 여기서 단건 진단을 불러 ⑦층을 확정해요. 목록은 그걸 못 해요(소비자 경로 Query 가
 * 사람별이라서). 부여 대상을 바꾸면 그 사람 기준으로 다시 진단해요.
 *
 * 그 확정 결과를 `onObserved` 로 목록에 올려요. 목록 점과 이 패널이 **같은 관측 하나**를
 * 보게 하려는 거예요 — 서로 다른 출처를 보면 「패널은 초록인데 점은 회색」이 생기고, 관리자가
 * 부여가 됐는지 판단할 수 없어요.
 */
function GrantPanel({
  row,
  refreshToken,
  onChanged,
  onObserved,
}: {
  row: AuthorizationRequestRow;
  onChanged: () => Promise<unknown> | void;
  refreshToken: number;
  onObserved: (steps: ChainLayerStatus[], subject: string) => void;
}) {
  const target: ChainTarget = {
    agentId: row.agentId,
    assetId: row.assetId,
    assetVersion: row.assetVersion,
    operationId: row.operationId,
  };
  const grantDefault = defaultGrantPrincipal(row);
  const [mode, setMode] = useState<"principal" | "group">("principal");
  const [principalId, setPrincipalId] = useState(grantDefault.principalId);
  const [principalLabel, setPrincipalLabel] = useState(grantDefault.label);
  const [group, setGroup] = useState<PlatformGrantGroup>("admin");
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState("");
  const [feedback, setFeedback] = useState<Feedback>();

  const diagnosis = useSWR<AuthorizationRequestDiagnosis>(
    // 세대 토큰(`#N`)은 「다시 조회」가 올려요 — 새 키에는 캐시가 없어서 옛 관측을 올릴 수 없어요.
    `${CACHE_KEY}/${rowKey(row)}?${mode === "principal" ? principalId : ""}#${refreshToken}`,
    () =>
      diagnoseAuthorizationRequest(
        target,
        mode === "principal" ? principalId : undefined,
      ),
  );
  // 그룹 멤버 수는 폭발 반경 문구에 들어가요. 실패하면 0 으로 두고 문구가 「지금 없음」으로
  // 말해요 — 숫자를 못 읽었다고 부여를 막지는 않아요.
  const members = useSWR(
    mode === "group" ? `admin/identity/users?group=${group}` : null,
    () => listCognitoUsers({ group }),
  );

  // 진단이 «도착한 뒤» 목록 점에 올려요. `steps` 는 SWR 캐시의 같은 객체라 재검증 전까지
  // 참조가 유지되고, 부모의 setter 는 같은 참조면 상태를 그대로 돌려줘서 루프가 없어요.
  //
  // ⚠️ **어느 주체에 대한 관측인지 함께 올려요.** 서버는 ⑦ 를
  // `principal_id or binding.created_by` 로 판정하므로 이 진단은 «그 주체» 에 대한 답이에요.
  // 주체를 안 실어 보내면, 관리자가 부여 대상을 그룹이나 다른 사람으로 바꿔 관측한 초록이
  // 목록에서 「신청자가 부를 수 있다」로 읽혀요 — 신청자는 아직 못 부르는데요. 그게 IH-127
  // 계열이에요. 부모가 주체를 보고 신청자 기준일 때만 색을 올려요.
  const observedSteps = diagnosis.data?.steps;
  const observedSubject = mode === "principal" ? principalId : `group:${group}`;
  useEffect(() => {
    if (observedSteps !== undefined) onObserved(observedSteps, observedSubject);
  }, [observedSteps, observedSubject, onObserved]);

  const action = nextAction(row);
  // 폭발 반경 문구는 **도구 이름**으로 말해요. 예전엔 connection 요약을 받아 「이 권한 그룹을
  // 쓰는 N개 자산이 함께 열려요」로 말했는데, 그 그룹도 그 필드도 이제 없어요(ADR-0099).
  const toolName = toolLabel(
    row.assetObservation === "observed" && row.assetFound === true
      ? row.assetName
      : row.assetId,
    row.operationId,
  );
  const subject: GrantSubject | null =
    mode === "group"
      ? { subjectGroup: group }
      : principalId
        ? { principalId }
        : null;

  async function run(work: () => Promise<Feedback>) {
    setBusy(true);
    setFailure("");
    setFeedback(undefined);
    try {
      setFeedback(await work());
      await onChanged();
      await diagnosis.mutate();
    } catch (caught) {
      setFailure(errorMessage(caught, "요청을 처리하지 못했어요."));
    } finally {
      setBusy(false);
    }
  }

  function submit(current: ChainAction) {
    if (!subject) return;
    if (current === "grant_human") {
      return run(async () => {
        const result = await provisionChainAccessGrant(target, subject);
        const who =
          result.subject.kind === "group"
            ? `«${result.subject.id}» 그룹`
            : "부여 대상";
        return {
          tone: "ok",
          message:
            result.outcome === "unchanged"
              ? `${who}은 이미 «${toolName}» 을 부를 수 있어요.`
              : `${who}에게 «${toolName}» 을 열었어요. 이 권한은 이 도구 하나에만 닿아요.`,
        };
      });
    }
    return run(async () => {
      const result: ChainApprovalResult = await approveAuthorizationChain(
        target,
        subject,
      );
      return {
        tone: chainApprovalTone(result),
        message: chainApprovalMessage(result),
      };
    });
  }

  return (
    <div className="space-y-3">
      {/* 두 단계의 사유를 한 줄씩. 목록의 진행선은 색만 보여주고, 이유는 여기 있어요. */}
      <ol className="grid gap-1.5 sm:grid-cols-2">
        {CHAIN_LAYERS.map((definition) => {
          const status =
            stepFor(diagnosis.data?.steps ?? [], definition.layer) ??
            stepFor(row.steps, definition.layer);
          const tone: LayerTone = status ? layerTone(status.state) : "unknown";
          return (
            <li key={definition.layer} className="flex min-w-0 items-start gap-2">
              <Badge
                variant="type"
                className={cn(
                  "mt-0.5 shrink-0",
                  tone === "ok"
                    ? "bg-emerald-100 text-emerald-700"
                    : tone === "todo"
                      ? "bg-amber-100 text-amber-800"
                      : tone === "blocked"
                        ? "bg-red-100 text-red-700"
                        : "bg-slate-200 text-slate-700",
                )}
              >
                {TONE_LABEL[tone]}
              </Badge>
              <div className="min-w-0">
                <div className="text-[12px] font-medium">{definition.title}</div>
                <p className="break-words text-[11.5px] text-muted-foreground">
                  {status ? layerMessage(status) : definition.hint}
                </p>
              </div>
            </li>
          );
        })}
      </ol>

      {/* ⑦칸의 판정은 «어느 주체» 기준인지 밝혀야 해요 (2026-09-06 브라우저 e2e).
          서버는 `principal_id or binding.created_by` 를 주체로 잡고 **그 주체의 그룹**까지
          봐요(`access_router.diagnose_authorization_request`). 그래서 「그룹에게」로 바꿔 다른
          그룹을 골라도 이 판정은 여전히 신청자 기준이고, 고른 그룹은 확인되지 않아요 —
          라벨 없이 「충족」만 보여주면 화면이 「그 그룹이 부를 수 있다」는 거짓을 말해요.
          진단 요청에 그룹을 실을 수는 없어요(라우트가 `principal_id` 만 받아요). 그래서 «측정한
          것을 그대로 적는» 쪽으로 닫아요. */}
      {diagnosis.data && (
        <p className="text-[11.5px] text-muted-foreground">
          위 「사람 권한」 판정은{" "}
          <span className="font-medium">{diagnosis.data.subjectPrincipalId || "신청자"}</span>
          {diagnosis.data.subjectGroups.length > 0 && (
            <> 와 그 사람의 그룹({diagnosis.data.subjectGroups.join(" · ")})</>
          )}{" "}
          기준이에요.
          {mode === "group" && (
            <span className="font-medium text-amber-800">
              {" "}
              아래에서 고른 «{toolGrantGroupLabel(group)}» 그룹을 따로 확인한 값이 아니에요 —
              그 그룹 권한은 «도구 호출 주체» 화면에서 확인해요.
            </span>
          )}
        </p>
      )}

      <SensitivityDisclosure
        sensitivity={diagnosis.data?.sensitivity ?? row.sensitivity}
        source={diagnosis.data?.sensitivitySource ?? row.sensitivitySource}
        status={diagnosis.data?.sensitivityStatus ?? row.sensitivityStatus}
      />

      <div className="border-t border-[#bfdbfe] pt-3 text-[11.5px] text-muted-foreground">
        {agentLabel(row)} <span aria-hidden>→</span> {assetLabel(row)} ·{" "}
        <span className="font-mono">{row.gatewayAction}</span> · 신청자{" "}
        {requesterLabel(row)}
      </div>

      {isActionable(action) && (
        <div className="space-y-3 rounded-lg border border-[#bfdbfe] bg-white p-3">
          <div className="flex flex-wrap items-center gap-2 text-[12.5px] font-semibold">
            사람 권한 — 누가 쓸 수 있게 할까요?
            <HelpPopover label="사람 권한 설명">
              이 도구를 호출하려면 호출하는 <strong>사람</strong>에게 이 도구의 권한이 있어야
              해요. agent 가 도구를 가졌다고 아무나 부를 수 있는 게 아니에요 — 이게 유일한
              사람 단위 검사예요. 도구 축으로 한꺼번에 보고 싶으면 «도구 호출 주체» 화면에서
              같은 권한을 관리할 수 있어요.
            </HelpPopover>
          </div>

          <div className="inline-flex overflow-hidden rounded-lg border border-input">
            {(["principal", "group"] as const).map((value) => (
              <button
                key={value}
                type="button"
                onClick={() => setMode(value)}
                className={cn(
                  "px-3.5 py-1.5 text-[12.5px] font-semibold",
                  mode === value ? "bg-foreground text-background" : "bg-card text-muted-foreground",
                )}
              >
                {value === "principal" ? "사람에게" : "그룹에게"}
              </button>
            ))}
          </div>

          <div className="grid gap-4 lg:grid-cols-2">
            <div>
              {mode === "principal" ? (
                <PrincipalPicker
                  rowKey={rowKey(row)}
                  label={principalLabel}
                  mustPick={grantDefault.mustPick}
                  isDefault={
                    !grantDefault.mustPick && principalId === grantDefault.principalId
                  }
                  onPick={(user) => {
                    setPrincipalId(user.sub);
                    setPrincipalLabel(user.email || user.sub);
                  }}
                  onReset={
                    grantDefault.mustPick
                      ? undefined
                      : () => {
                          setPrincipalId(grantDefault.principalId);
                          setPrincipalLabel(grantDefault.label);
                        }
                  }
                />
              ) : (
                <div>
                  <div className="mb-1.5 flex items-center gap-1 text-[12px] font-medium">
                    그룹
                    <HelpPopover label="선택 가능한 그룹 설명">
                      Agora 는 <code>user</code>·<code>admin</code> 두 그룹만 인가에 써요.
                      다른 Cognito 그룹은 호출 시점 판정에 실리지 않아서, 부여해도 권한이
                      동작하지 않아요 — 서버도 422 로 막아요.
                    </HelpPopover>
                  </div>
                  <div className="flex flex-wrap gap-2">
                    {PLATFORM_GRANT_GROUPS.map((value) => (
                      <button
                        key={value}
                        type="button"
                        onClick={() => {
                          if (isPlatformGrantGroup(value)) setGroup(value);
                        }}
                        className={cn(
                          "rounded-full border px-3 py-1 text-[12px] font-semibold",
                          group === value
                            ? "border-primary text-primary"
                            : "border-border text-muted-foreground",
                        )}
                      >
                        {value}
                        <span className="ml-1 font-normal">
                          {toolGrantGroupLabel(value)}
                        </span>
                      </button>
                    ))}
                  </div>
                  <p className="mt-2 text-[11.5px] text-muted-foreground">
                    그룹 권한은 <strong>한 행</strong>이에요. 사람마다 만들지 않아요.
                  </p>
                </div>
              )}
            </div>
            <div>
              <div className="mb-1.5 flex items-center gap-1 text-[12px] font-medium">
                이 권한이 닿는 범위
                <HelpPopover label="폭발 반경 설명">
                  grant 하나가 정확히 <strong>도구 하나</strong>를 열어요 — 키가{" "}
                  <code>(주체, asset_id, operation_id)</code> 라 다른 자산으로 번지지 않아요
                  (ADR-0099 결정 2). 남는 반경은 <strong>사람 축</strong>이에요: 그룹에 주면
                  지금 멤버와 앞으로 들어오는 사람까지 부를 수 있어요.
                </HelpPopover>
              </div>
              <p
                className={cn(
                  "rounded-md border px-3 py-2 text-[11.5px]",
                  mode === "group"
                    ? "border-red-300 bg-red-50 text-red-900"
                    : "border-amber-300 bg-amber-50 text-amber-900",
                )}
              >
                {blastRadiusMessage(
                  toolName,
                  mode === "group"
                    ? {
                        kind: "group",
                        groupLabel: toolGrantGroupLabel(group),
                        memberCount: members.data?.items.length ?? 0,
                      }
                    : { kind: "principal" },
                ) || "어느 도구를 여는지 확인하는 중이에요."}
              </p>
            </div>
          </div>

          <div className="flex flex-wrap items-center gap-2 border-t border-[#bfdbfe] pt-3">
            <Button
              size="sm"
              onClick={() => submit(action)}
              disabled={busy || !subject}
              title={!subject ? "권한을 받을 사람을 먼저 골라 주세요." : undefined}
            >
              {busy ? "처리 중…" : ACTION_LABELS[action]}
            </Button>
            {failure && <InlineError message={failure} />}
            <span className="ml-auto inline-flex items-center gap-1 text-[11px] text-muted-foreground">
              함께 생기는 것: binding 승인 · 사람 권한
              <HelpPopover label="한 번에 채워지는 것 설명">
                두 칸을 한 번에 채워요. 중간에 실패하면 어디까지 됐는지 알려줘요 — 원인을
                고치고 다시 누르면 남은 것만 이어서 해요(멱등).
              </HelpPopover>
            </span>
          </div>
        </div>
      )}
      {!isActionable(action) && (
        <p
          role={
            action === "revoke_orphan" ||
            action === "asset_not_approved" ||
            action === "asset_observation_unknown"
              ? "alert"
              : undefined
          }
          className={cn(
            "text-[12.5px] text-muted-foreground",
            (action === "revoke_orphan" ||
              action === "asset_not_approved" ||
              action === "asset_observation_unknown") &&
              "rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-amber-900",
          )}
        >
          {ACTION_LABELS[action]}
        </p>
      )}

      {feedback && (
        <div
          role={feedback.tone === "warn" ? "alert" : "status"}
          className={cn(
            "rounded-md border px-3 py-2 text-[12.5px]",
            feedback.tone === "ok"
              ? "border-emerald-300 bg-emerald-50 text-emerald-900"
              : feedback.tone === "pending"
                ? "border-blue-300 bg-blue-50 text-blue-900"
                : "border-amber-300 bg-amber-50 text-amber-900",
          )}
        >
          {feedback.message}
        </div>
      )}
    </div>
  );
}
