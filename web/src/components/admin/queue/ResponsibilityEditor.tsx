"use client";

import { useState } from "react";
import useSWR from "swr";

import {
  ApiError,
  getAssetResponsibility,
  listAssetResponsibilityHistory,
  listResponsibilityMembers,
  updateAssetResponsibility,
  type ApprovalBlock,
  type DirectoryPage,
  type ResponsibilityChange,
  type ResponsibilityContacts,
  type ResponsibilityHistory,
  type ResponsibilityStatus,
} from "@/lib/api";
import {
  isContactEmail,
  responsibilityEditorStartsOpen,
  responsibilitySaveIsComplete,
  responsibilityUpdateProblem,
} from "@/lib/responsibilityContacts";
import { cn } from "@/lib/ui";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Icon } from "@/components/ui/icon";

const CONFLICT_MESSAGE =
  "책임자 정보가 동시에 변경됐어요. 최신 값을 조회한 뒤 다시 시도하세요.";
const MEMBER_RESULT_LIMIT = 10;

const BLOCKING_REASON_LABELS: Record<string, string> = {
  owner_contact_required: "1차 담당자가 지정되지 않았어요.",
  owner_contact_invalid: "1차 담당자 email 형식이 올바르지 않아요.",
  escalation_contact_required: "2차 담당자가 지정되지 않았어요.",
  escalation_contact_invalid: "2차 담당자 email 형식이 올바르지 않아요.",
  distinct_escalation_contact_required:
    "1차와 2차 담당자는 서로 달라야 해요.",
};

type SaveNotice = {
  tone: "success" | "warning";
  message: string;
};

type ResponsibilityEditorProps = {
  recordId: string;
  approvalBlockReason?: ApprovalBlock["reason"];
  canManage: boolean;
  onChanged: () => void | Promise<unknown>;
};

export function ResponsibilityEditor({
  recordId,
  approvalBlockReason,
  canManage,
  onChanged,
}: ResponsibilityEditorProps) {
  const [open, setOpen] = useState(() =>
    responsibilityEditorStartsOpen(approvalBlockReason),
  );
  const [formRevision, setFormRevision] = useState(0);
  const [saveNotice, setSaveNotice] = useState<SaveNotice | null>(null);
  const [reloadError, setReloadError] = useState("");
  const [reloading, setReloading] = useState(false);
  const [historyOpen, setHistoryOpen] = useState(false);

  const {
    data: status,
    error: statusError,
    isLoading: statusLoading,
    mutate: mutateStatus,
  } = useSWR<ResponsibilityStatus>(
    ["asset/responsibility", recordId],
    () => getAssetResponsibility(recordId),
    { revalidateOnFocus: false },
  );

  const {
    data: history,
    error: historyError,
    isLoading: historyLoading,
    mutate: mutateHistory,
  } = useSWR<ResponsibilityHistory>(
    canManage && historyOpen
      ? ["asset/responsibility/history", recordId]
      : null,
    () => listAssetResponsibilityHistory(recordId),
    { revalidateOnFocus: false },
  );

  const summary = responsibilitySummary(status, statusError, statusLoading);

  async function reloadStatus() {
    setReloadError("");
    setReloading(true);
    try {
      const fresh = await mutateStatus();
      if (fresh) {
        setFormRevision((revision) => revision + 1);
        setSaveNotice(null);
      }
    } catch (error) {
      setReloadError(
        errorMessage(error, "최신 담당자 정보를 조회하지 못했어요."),
      );
      throw error;
    } finally {
      setReloading(false);
    }
  }

  async function handleSaved(result: ResponsibilityStatus) {
    await mutateStatus(result, { revalidate: false });
    setFormRevision((revision) => revision + 1);
    setReloadError("");

    if (responsibilitySaveIsComplete(result)) {
      setSaveNotice({
        tone: "success",
        message: "담당자 연락 계약을 완료했어요.",
      });
    } else {
      const remaining =
        result.blocking_reasons.length > 0
          ? result.blocking_reasons.map(blockingReasonLabel).join(" ")
          : "계약 완료 상태를 확인하지 못했어요.";
      setSaveNotice({
        tone: "warning",
        message: `연락처는 반영됐지만 아직 보완이 필요해요. ${remaining}`,
      });
    }

    if (historyOpen) void mutateHistory().catch(() => undefined);
    void Promise.resolve(onChanged()).catch(() => undefined);
  }

  const panelId = `responsibility-editor-${recordId}`;

  return (
    <Card className="overflow-hidden">
      <button
        type="button"
        className="flex w-full items-start gap-3 px-5 py-4 text-left transition-colors hover:bg-muted/30 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring"
        aria-expanded={open}
        aria-controls={panelId}
        onClick={() => setOpen((current) => !current)}
      >
        <Icon name="users" size={18} className="mt-0.5 text-muted-foreground" />
        <span className="min-w-0 flex-1">
          <span className="flex flex-wrap items-center gap-2">
            <span className="text-sm font-semibold">담당자 연락처</span>
            <span
              className={cn(
                "rounded px-2 py-0.5 text-[11px] font-medium",
                summary.className,
              )}
            >
              {summary.label}
            </span>
          </span>
          <span className="mt-1 block break-all text-xs text-muted-foreground">
            {summary.detail}
          </span>
        </span>
        <Icon
          name="back"
          size={16}
          className={cn(
            "mt-1 text-muted-foreground transition-transform",
            open ? "-rotate-90" : "rotate-180",
          )}
        />
      </button>

      {open && (
        <div id={panelId} className="border-t border-border px-5 py-5">
          {statusLoading && !status ? (
            <div className="space-y-3" aria-label="담당자 정보 조회 중">
              <div className="h-10 animate-pulse rounded bg-muted/50" />
              <div className="h-24 animate-pulse rounded bg-muted/50" />
            </div>
          ) : statusError || !status ? (
            <LoadFailure
              message={errorMessage(
                statusError,
                "담당자 정보를 조회하지 못했어요.",
              )}
              retrying={reloading}
              retryError={reloadError}
              onRetry={reloadStatus}
            />
          ) : (
            <>
              <p className="mb-4 border-l-2 border-blue-300 pl-3 text-[13px] text-muted-foreground">
                이 변경은 운영 연락 인계이며 자산 소유권이나 인가 권한을
                이전하지 않아요.
              </p>

              {!responsibilitySaveIsComplete(status) && (
                <div
                  role="status"
                  className="mb-4 rounded-md border border-amber-200 bg-amber-50 px-3 py-2.5 text-[13px] text-amber-900"
                >
                  <p className="font-semibold">현재 남은 문제</p>
                  {status.blocking_reasons.length > 0 ? (
                    <ul className="mt-1 list-disc space-y-0.5 pl-5">
                      {status.blocking_reasons.map((reason) => (
                        <li key={reason}>{blockingReasonLabel(reason)}</li>
                      ))}
                    </ul>
                  ) : (
                    <p className="mt-1">
                      미완료 상태지만 상세 사유를 확인하지 못했어요. 최신 값을
                      다시 조회해 주세요.
                    </p>
                  )}
                </div>
              )}

              <ResponsibilityForm
                key={`${recordId}:${formRevision}:${status.owner_contact}:${status.escalation_contact}`}
                initialStatus={status}
                canManage={canManage}
                reloading={reloading}
                onEdit={() => setSaveNotice(null)}
                onReload={reloadStatus}
                onSaved={handleSaved}
              />

              {saveNotice && (
                <p
                  role="status"
                  aria-live="polite"
                  className={cn(
                    "mt-3 rounded-md border px-3 py-2 text-[13px]",
                    saveNotice.tone === "success"
                      ? "border-emerald-200 bg-emerald-50 text-emerald-800"
                      : "border-amber-200 bg-amber-50 text-amber-900",
                  )}
                >
                  {saveNotice.message}
                </p>
              )}
              {reloadError && (
                <p role="alert" className="mt-2 text-xs text-red-700">
                  최신 값 조회 실패: {reloadError}
                </p>
              )}

              <ResponsibilityHistorySection
                canManage={canManage}
                open={historyOpen}
                history={history}
                error={historyError}
                loading={historyLoading}
                onToggle={() => setHistoryOpen((current) => !current)}
                onRetry={() =>
                  void mutateHistory().catch(() => undefined)
                }
              />
            </>
          )}
        </div>
      )}
    </Card>
  );
}

function ResponsibilityForm({
  initialStatus,
  canManage,
  reloading,
  onEdit,
  onReload,
  onSaved,
}: {
  initialStatus: ResponsibilityStatus;
  canManage: boolean;
  reloading: boolean;
  onEdit: () => void;
  onReload: () => Promise<void>;
  onSaved: (result: ResponsibilityStatus) => Promise<void>;
}) {
  const [ownerContact, setOwnerContact] = useState(
    initialStatus.owner_contact,
  );
  const [escalationContact, setEscalationContact] = useState(
    initialStatus.escalation_contact,
  );
  const [reason, setReason] = useState("");
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState("");
  const [conflict, setConflict] = useState(false);

  const problem = responsibilityUpdateProblem({
    ownerContact,
    escalationContact,
    reason,
  });

  function changeOwner(value: string) {
    setOwnerContact(value);
    setSaveError("");
    setConflict(false);
    onEdit();
  }

  function changeEscalation(value: string) {
    setEscalationContact(value);
    setSaveError("");
    setConflict(false);
    onEdit();
  }

  async function save(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (problem || !canManage) return;

    setSaving(true);
    setSaveError("");
    setConflict(false);
    try {
      const result = await updateAssetResponsibility(
        initialStatus.record_id,
        {
          owner_contact: ownerContact.trim(),
          escalation_contact: escalationContact.trim(),
          reason: reason.trim(),
        },
      );
      await onSaved(result);
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) {
        setSaveError(CONFLICT_MESSAGE);
        setConflict(true);
      } else {
        // 422와 403은 서버의 구체적인 계약/권한 문구를 줄이지 않고 그대로 보여줘요.
        setSaveError(errorMessage(error, "담당자 정보를 저장하지 못했어요."));
      }
    } finally {
      setSaving(false);
    }
  }

  return (
    <form onSubmit={save}>
      <div className="grid gap-4 lg:grid-cols-2">
        <ContactPicker
          id="responsibility-owner-search"
          label="1차 담당자"
          description="운영 문의의 첫 연락 대상이에요."
          value={ownerContact}
          excludedEmail={escalationContact}
          disabled={!canManage || saving}
          onChange={changeOwner}
        />
        <ContactPicker
          id="responsibility-escalation-search"
          label="2차 담당자(에스컬레이션)"
          description="1차 담당자 부재 시 연락할 다른 구성원이에요."
          value={escalationContact}
          excludedEmail={ownerContact}
          disabled={!canManage || saving}
          onChange={changeEscalation}
        />
      </div>

      <div className="mt-4">
        <div className="mb-1.5 flex items-center justify-between gap-3">
          <label
            htmlFor="responsibility-change-reason"
            className="text-xs font-medium text-foreground"
          >
            변경 사유 *
          </label>
          <span className="text-[11px] text-muted-foreground">
            {reason.length}/1000
          </span>
        </div>
        <textarea
          id="responsibility-change-reason"
          value={reason}
          onChange={(event) => {
            setReason(event.target.value);
            setSaveError("");
            setConflict(false);
            onEdit();
          }}
          rows={3}
          maxLength={1000}
          required
          disabled={!canManage || saving}
          placeholder="예: 온콜 로테이션 변경에 따른 운영 인계"
          className="w-full resize-y rounded-lg border border-input bg-card px-3 py-2 text-sm placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-60"
        />
      </div>

      <div className="mt-3 flex flex-wrap items-start justify-between gap-3">
        <div className="min-h-5 flex-1">
          {!canManage ? (
            <p className="text-xs text-amber-700">
              담당자 변경과 이력 조회는 admin 전용이에요.
            </p>
          ) : problem ? (
            <p className="text-xs text-amber-700">{problem}</p>
          ) : null}
          {saveError && (
            <div
              role="alert"
              className="mt-1 flex flex-wrap items-center gap-2 text-xs text-red-700"
            >
              <span>{saveError}</span>
              {conflict && (
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  disabled={reloading}
                  onClick={() => void onReload().catch(() => undefined)}
                >
                  <Icon name="refresh" size={13} />
                  {reloading ? "조회 중…" : "최신 값 재조회"}
                </Button>
              )}
            </div>
          )}
        </div>
        <Button
          type="submit"
          size="sm"
          disabled={!canManage || saving || problem !== null}
        >
          <Icon name="check" size={14} />
          {saving ? "저장 중…" : "담당자 저장"}
        </Button>
      </div>
    </form>
  );
}

function ContactPicker({
  id,
  label,
  description,
  value,
  excludedEmail,
  disabled,
  onChange,
}: {
  id: string;
  label: string;
  description: string;
  value: string;
  excludedEmail: string;
  disabled: boolean;
  onChange: (email: string) => void;
}) {
  const [query, setQuery] = useState("");
  const needle = query.trim();
  const canSearch = !disabled && !value && needle.length >= 2;
  // 질의를 key에 넣지 않는 이유: admin 본인 포함을 위해 필요한 전체 page 조회를 두 picker와
  // 모든 검색어가 한 번만 공유해요. revalidateIfStale=false라 2자를 다시 입력해도 재조회하지 않아요.
  //
  // ⛔ 이 키를 `ALL_USERS_SWR_KEY`(`lib/api/users.ts`)로 «합치지 마세요» — IH-164 는
  // Cognito 전수 walk 키를 셋만 합치고 이 키는 일부러 남겼어요. 합치면 셋이 바뀌어요:
  //  ⑴ fetcher 가 `listResponsibilityMembers` → `listAllCognitoUsers` 로 바뀌면서
  //     미관측 공시 `truncated` 가 사라져요. `listAllCognitoUsers` 는 커서가 반복돼도
  //     그냥 루프를 빠져나와 `CognitoUser[]` 만 돌려주니, 아래 「회원 디렉터리 전체를
  //     확인하지 못했어요」 문구가 렌더될 근거 자체가 없어져요 — 부분 목록이
  //     「전체를 확인했다」로 보여요 (AGENTS.md 「Unobservable is not passing」).
  //  ⑵ `listResponsibilityMembers` 의 `status === "DISABLED"` 제외가 없어져
  //     비활성 회원이 2차 담당자 후보로 선택 가능해져요.
  //  ⑶ 이 키는 `canSearch`(2자 이상 입력) 조건부라 walk 가 «지연»돼요. 통합 키는
  //     무조건 walk 라, 합치면 화면 진입 즉시 walk 가 돌아 IH-164 가 줄이려는 비용을
  //     오히려 늘려요.
  const { data, error, isLoading } = useSWR<DirectoryPage>(
    canSearch ? "admin/identity/users/all-for-responsibility" : null,
    listResponsibilityMembers,
    { revalidateIfStale: false, revalidateOnFocus: false },
  );

  const activeMembers = (data?.items ?? []).filter((member) =>
    isContactEmail(member.email),
  );
  const normalizedNeedle = needle.toLocaleLowerCase();
  const matched = canSearch
    ? activeMembers.filter((member) =>
        [member.name, member.email].some((value) =>
          value.toLocaleLowerCase().includes(normalizedNeedle),
        ),
      )
    : [];
  const excluded = excludedEmail.trim().toLowerCase();
  // 호출자인 admin은 제외하지 않아요. 이 화면의 계약은 두 연락처가 서로 다른지만 봐요.
  const selectable = matched.filter(
    (user) => user.email.trim().toLowerCase() !== excluded,
  );
  const visible = selectable.slice(0, MEMBER_RESULT_LIMIT);
  const onlyExcluded =
    matched.length > 0 && selectable.length === 0 && Boolean(excluded);

  return (
    <div className="min-w-0">
      {value ? (
        <p className="text-xs font-medium text-foreground">{label} *</p>
      ) : (
        <label htmlFor={id} className="text-xs font-medium text-foreground">
          {label} *
        </label>
      )}
      {value ? (
        <div className="mt-1.5 flex min-w-0 items-center gap-2">
          <span className="min-w-0 flex-1 break-all rounded-lg border border-border bg-muted/30 px-3 py-2 text-sm">
            {value}
          </span>
          <Button
            type="button"
            variant="outline"
            size="sm"
            disabled={disabled}
            onClick={() => {
              onChange("");
              setQuery("");
            }}
          >
            <Icon name="refresh" size={13} />
            변경
          </Button>
        </div>
      ) : (
        <>
          <div className="relative mt-1.5">
            <Icon
              name="search"
              size={14}
              className="pointer-events-none absolute left-3 top-2.5 text-muted-foreground"
            />
            <input
              id={id}
              type="search"
              value={query}
              disabled={disabled}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="이름 또는 email 검색 (2자 이상)"
              autoComplete="off"
              className="h-9 w-full rounded-lg border border-input bg-card pl-9 pr-3 text-sm placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-60"
            />
          </div>
          {canSearch && isLoading && (
            <p className="mt-1 text-xs text-muted-foreground">검색 중…</p>
          )}
          {canSearch && error && (
            <p role="alert" className="mt-1 text-xs text-red-700">
              회원을 검색하지 못했어요:{" "}
              {errorMessage(error, "알 수 없는 오류")}
            </p>
          )}
          {canSearch && !isLoading && !error && matched.length === 0 && (
            <p className="mt-1 text-xs text-muted-foreground">
              검색 결과가 없어요.
            </p>
          )}
          {canSearch && !isLoading && !error && onlyExcluded && (
            <p className="mt-1 text-xs text-amber-700">
              1차와 2차 담당자는 달라야 해요. 다른 회원을 검색해 주세요.
            </p>
          )}
          {visible.length > 0 && (
            <ul className="mt-1 max-h-48 divide-y divide-border overflow-auto rounded-lg border border-border bg-card">
              {visible.map((user) => (
                <li key={user.email}>
                  <button
                    type="button"
                    className="flex w-full min-w-0 flex-col items-start px-3 py-2 text-left hover:bg-muted/50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring"
                    onClick={() => {
                      onChange(user.email.trim().toLowerCase());
                      setQuery("");
                    }}
                  >
                    <span className="max-w-full truncate text-sm text-foreground">
                      {user.name || user.email}
                    </span>
                    <span className="max-w-full truncate text-xs text-muted-foreground">
                      {user.email}
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          )}
          {selectable.length > MEMBER_RESULT_LIMIT && (
            <p className="mt-1 text-xs text-amber-700">
              결과가 많아 10명까지만 보여줘요. 검색어를 더 좁혀 주세요.
            </p>
          )}
          {canSearch && data?.truncated && (
            <p className="mt-1 text-xs text-amber-700">
              회원 디렉터리 전체를 확인하지 못했어요. 검색 결과에 없는 회원이
              있을 수 있어요.
            </p>
          )}
        </>
      )}
      <p className="mt-1 text-xs text-muted-foreground">{description}</p>
    </div>
  );
}

function ResponsibilityHistorySection({
  canManage,
  open,
  history,
  error,
  loading,
  onToggle,
  onRetry,
}: {
  canManage: boolean;
  open: boolean;
  history?: ResponsibilityHistory;
  error: unknown;
  loading: boolean;
  onToggle: () => void;
  onRetry: () => void;
}) {
  return (
    <section className="mt-5 border-t border-border pt-4">
      <div className="flex items-center justify-between gap-3">
        <div>
          <h3 className="text-sm font-semibold">변경 이력</h3>
          <p className="mt-0.5 text-xs text-muted-foreground">
            변경 시각, 주체, 사유와 이전·이후 연락처를 확인해요.
          </p>
        </div>
        <Button
          type="button"
          variant="outline"
          size="sm"
          disabled={!canManage}
          aria-expanded={open}
          onClick={onToggle}
        >
          <Icon name="scroll" size={14} />
          {open ? "이력 닫기" : "이력 보기"}
        </Button>
      </div>

      {open && (
        <div className="mt-3">
          {loading && !history ? (
            <p className="text-xs text-muted-foreground">이력을 읽는 중…</p>
          ) : error ? (
            <div className="flex flex-wrap items-center gap-2 text-xs text-red-700">
              <span>
                이력을 읽지 못했어요:{" "}
                {errorMessage(error, "알 수 없는 오류")}
              </span>
              <Button type="button" variant="outline" size="sm" onClick={onRetry}>
                <Icon name="refresh" size={13} />
                다시 조회
              </Button>
            </div>
          ) : !history || history.items.length === 0 ? (
            <p className="text-xs text-muted-foreground">
              아직 담당자 변경 이력이 없어요.
            </p>
          ) : (
            <ul className="divide-y divide-border border-y border-border">
              {history.items.map((event) => (
                <ResponsibilityHistoryItem
                  key={event.event_id}
                  event={event}
                />
              ))}
            </ul>
          )}
        </div>
      )}
    </section>
  );
}

function ResponsibilityHistoryItem({
  event,
}: {
  event: ResponsibilityChange;
}) {
  return (
    <li className="py-3 text-xs">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
        <time
          dateTime={event.changed_at}
          className="font-medium text-foreground"
        >
          {formatKst(event.changed_at)}
        </time>
        <span
          className="font-mono text-muted-foreground"
          title={event.changed_by}
        >
          변경자 {shortPrincipal(event.changed_by)}
        </span>
      </div>
      <p className="mt-1 break-words text-muted-foreground">
        사유: {event.reason}
      </p>
      <div className="mt-2 grid gap-2 sm:grid-cols-[1fr_auto_1fr] sm:items-center">
        <ContactSnapshot label="이전" contacts={event.before} />
        <span className="hidden text-muted-foreground sm:inline">→</span>
        <ContactSnapshot label="이후" contacts={event.after} />
      </div>
    </li>
  );
}

function ContactSnapshot({
  label,
  contacts,
}: {
  label: string;
  contacts: ResponsibilityContacts;
}) {
  return (
    <div className="min-w-0 rounded-md bg-muted/40 px-3 py-2">
      <span className="font-semibold text-foreground">{label}</span>
      <span className="mt-1 block break-all text-muted-foreground">
        1차 {contacts.owner_contact || "미지정"}
      </span>
      <span className="block break-all text-muted-foreground">
        2차 {contacts.escalation_contact || "미지정"}
      </span>
    </div>
  );
}

function LoadFailure({
  message,
  retrying,
  retryError,
  onRetry,
}: {
  message: string;
  retrying: boolean;
  retryError: string;
  onRetry: () => Promise<void>;
}) {
  return (
    <div role="alert" className="text-sm text-red-700">
      <p className="font-medium">담당자 정보를 조회하지 못했어요.</p>
      <p className="mt-1 text-xs">{message}</p>
      <Button
        type="button"
        variant="outline"
        size="sm"
        className="mt-3"
        disabled={retrying}
        onClick={() => void onRetry().catch(() => undefined)}
      >
        <Icon name="refresh" size={13} />
        {retrying ? "조회 중…" : "다시 조회"}
      </Button>
      {retryError && <p className="mt-2 text-xs">{retryError}</p>}
    </div>
  );
}

function responsibilitySummary(
  status: ResponsibilityStatus | undefined,
  error: unknown,
  loading: boolean,
): { label: string; detail: string; className: string } {
  if (error) {
    return {
      label: "조회 실패",
      detail: "현재 값을 확인하지 못했어요. 펼쳐서 오류를 확인하세요.",
      className: "bg-red-100 text-red-800",
    };
  }
  if (loading || !status) {
    return {
      label: "조회 중",
      detail: "현재 담당자 상태를 확인하고 있어요.",
      className: "bg-slate-100 text-slate-600",
    };
  }
  const contacts = `1차 ${status.owner_contact || "미지정"} · 2차 ${
    status.escalation_contact || "미지정"
  }`;
  if (responsibilitySaveIsComplete(status)) {
    return {
      label: "완료",
      detail: contacts,
      className: "bg-emerald-100 text-emerald-800",
    };
  }
  return {
    label: "보완 필요",
    detail: contacts,
    className: "bg-amber-100 text-amber-800",
  };
}

function blockingReasonLabel(reason: string): string {
  return BLOCKING_REASON_LABELS[reason] ?? `확인 필요: ${reason}`;
}

function errorMessage(error: unknown, fallback: string): string {
  if (error instanceof ApiError || error instanceof Error) return error.message;
  return fallback;
}

function formatKst(value: string): string {
  try {
    return new Intl.DateTimeFormat("ko-KR", {
      timeZone: "Asia/Seoul",
      dateStyle: "medium",
      timeStyle: "short",
    }).format(new Date(value));
  } catch {
    return value;
  }
}

function shortPrincipal(value: string): string {
  const principal = value.trim();
  if (principal.length <= 16) return principal || "알 수 없음";
  // 표시용 actor label은 CA-35에서 함께 해결해요. 여기서는 sub 원문을 title에 보존해요.
  return `${principal.slice(0, 8)}…${principal.slice(-4)}`;
}
