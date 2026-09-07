"use client";

import { useState } from "react";
import useSWR from "swr";
import {
  createAccessConnection,
  listAccessCapabilityList,
  listAccessConnections,
  listAccessGrants,
  putAccessCapabilities,
  seedCapabilityPresets,
  updateAccessConnection,
  type AccessCapability,
  type AccessCapabilityList,
  type AccessConnection,
  type AccessGrant,
} from "@/lib/api";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input, Select } from "@/components/ui/input";
import { Icon } from "@/components/ui/icon";
import { useConfirm } from "@/components/ui/confirm-dialog";
import { useToast } from "@/components/ui/toast";
import {
  EmptyState,
  Field,
  FormActions,
  InlineError,
  LoadError,
  LoadingRows,
  PageHeading,
  SectionHeading,
  StatusBadge,
  capsOf,
  errorMessage,
  isConflict,
  parseList,
  textAreaClass,
} from "@/components/admin/access/shared";
import {
  accessCapabilityCacheKey,
  accessCapabilityItems,
} from "@/lib/adminAccessCapabilities";
import { cn } from "@/lib/ui";
import { ResyncPanel } from "./ResyncPanel";

// 권한 그룹 정의 (/admin/capability-sets) — 기존 access/ 의 connections 탭을
// 분리하고 의미를 재정의했어요. 레코드는 동일한 CONNECTION#<id> 지만
// "업무 시스템 연결"이 아니라 "MCP tool 이 요구하는 권한 그룹"으로 읽어요
// (docs/design/admin-console-spec.md §4.5).
//
// UI 에서 숨기는 필드 5개 — 어떤 코드도 읽지 않고, DB·SaaS 직결이라는 잘못된 그림을
// 만들어요. 필드를 지우지는 않아요(기존 레코드가 그대로 읽혀야 하고 백엔드 변경 0이 목표).
//   kind            → "capability_set" 고정
//   credential_mode → "mcp" 고정
//   target          → 그룹 이름 (ConnectionCreate 필수값이라 이름을 재사용)
//   role_arn        → 전송 생략
//   external_id_ref → 전송 생략
//   enforcement     → 기본값(fine_grained). 판정은 항상 fine_grained 처럼 동작해요
//
// 시안에는 "설명" 입력이 있지만 Connection 레코드에 설명 필드가 없어요. 입력을 조용히
// 버리거나 target 을 설명 저장소로 돌려쓰는 대신, 설계(§4.5)대로 target=이름만 보내고
// 설명 입력은 두지 않아요. capability 별 description 이 이미 의미를 담아요.
const FIXED_KIND = "capability_set";
const FIXED_CREDENTIAL_MODE = "mcp";

export function CapSetsClient() {
  const {
    data: groups,
    error,
    isLoading,
    mutate,
  } = useSWR<AccessConnection[]>("admin/access/connections", listAccessConnections);
  const [selectedId, setSelectedId] = useState("");
  const [creating, setCreating] = useState(false);
  const selected =
    groups?.find((item) => item.connection_id === selectedId) ?? groups?.[0] ?? null;

  const { success, error: toastError } = useToast();
  const [seeding, setSeeding] = useState(false);

  async function loadPresets() {
    setSeeding(true);
    try {
      const r = await seedCapabilityPresets();
      await mutate();
      success(
        "예제 권한 그룹을 불러왔어요.",
        `새로 ${r.created.length}개 추가, ${r.skipped.length}개는 이미 있어 건너뛰었어요.`,
      );
    } catch {
      toastError("예제 권한 그룹을 불러오지 못했어요.");
    } finally {
      setSeeding(false);
    }
  }

  return (
    <div className="min-w-0">
      <PageHeading
        title="권한 그룹 정의 (인가 미사용)"
        description="여기서 정의하는 capability 라벨과 권한 그룹은 더 이상 도구 인가에 쓰이지 않아요."
      />

      {/* ADR-0099 — capability 라벨(⑤)과 connection ceiling(⑥)이 인가 경로에서 빠졌어요.
          화면을 지우지 않는 이유는 이 화면이 `Connection` 레코드를 **생성·편집할 수 있는 유일한
          화면**이기 때문이에요(ADR-0014 · ADR-0112) — 기존 레코드 조회·되돌림 경로가 여기를
          봐야 해요. 완전 제거는 별 티켓이에요.

          ⚠️ 옛 주석은 「STS credential broker 가 읽는다」를 보존 근거로 적었는데 그건 **미검증**
          이었어요. 2026-09-05 실측: `issue_credentials`·`get_credential_broker` 의 호출부가
          테스트뿐이고 어떤 라우터도 `credential_broker` 를 import 하지 않아요 — broker 는 **어떤
          프로덕션 경로에도 배선돼 있지 않아요.**

          배너를 두는 이유는 「승인은 초록불인데 호출이 거부돼요」를 고치려고 관리자가 여기로 오는
          걸 막는 거예요 — 여기서 무엇을 바꿔도 도구 인가는 달라지지 않아요. */}
      <div
        role="alert"
        className="mb-5 rounded-lg border border-amber-300 bg-amber-50 p-3.5 text-xs text-amber-900"
      >
        <b className="block text-[13px]">
          이 화면은 더 이상 도구 인가에 영향을 주지 않아요 (ADR-0099)
        </b>
        <p className="mt-1 leading-relaxed">
          도구 인가는 두 층이에요 — <b>agent 의 도구 승인</b>(«도구 인가 승인» 화면)과{" "}
          <b>사람·그룹의 도구 권한</b>(«도구 호출 주체» 화면). capability 라벨과 권한 그룹은 그
          판정에서 빠졌어요. 여기서 상한을 넓히거나 좁혀도 어떤 도구도 열리거나 막히지 않아요.
        </p>
        <p className="mt-1 leading-relaxed">
          화면을 남겨 둔 건 <code>Connection</code> 레코드를 <b>만들고 고칠 수 있는 유일한 화면</b>
          이라서예요 — 기존 레코드를 보거나 되돌리려면 여기가 필요해요. 이 레코드의{" "}
          <code>role_arn</code>·<code>credential_mode</code> 를 읽는 자격증명 중개(STS broker)는{" "}
          <b>아직 어떤 프로덕션 경로에서도 쓰이지 않아요</b>.
        </p>
      </div>

      {isLoading ? (
        <LoadingRows label="권한 그룹 불러오는 중" />
      ) : error ? (
        <LoadError message="권한 그룹을 불러오지 못했어요." />
      ) : (
        <div className="space-y-6">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <SectionHeading
              title="권한 그룹"
              description="그룹 하나가 사용자·tool 에 배정되는 한 단위예요."
            />
            <div className="flex items-center gap-2">
              <Button size="sm" variant="outline" onClick={loadPresets} disabled={seeding}>
                <Icon name="plus" size={14} />
                {seeding ? "불러오는 중…" : "예제 권한 그룹 불러오기"}
              </Button>
              <Button size="sm" onClick={() => setCreating((value) => !value)}>
                <Icon name={creating ? "back" : "plus"} size={14} />
                {creating ? "목록으로" : "권한 그룹 추가"}
              </Button>
            </div>
          </div>

          {creating ? (
            <CapSetCreateForm
              onCreated={async (created) => {
                setCreating(false);
                setSelectedId(created.connection_id);
                await mutate();
              }}
              onCancel={() => setCreating(false)}
            />
          ) : groups?.length === 0 ? (
            <EmptyState
              title="정의된 권한 그룹이 없어요."
              description="첫 그룹을 만들면 Agent × Tool 매트릭스와 사용자 권한에서 고를 수 있어요."
              action={
                <Button size="sm" onClick={() => setCreating(true)}>
                  <Icon name="plus" size={14} />
                  첫 권한 그룹 추가
                </Button>
              }
            />
          ) : (
            <div className="grid min-w-0 gap-5 xl:grid-cols-[minmax(240px,0.72fr)_minmax(0,1.6fr)]">
              <div className="min-w-0 divide-y divide-border rounded-lg border border-border">
                {groups?.map((group) => (
                  <button
                    key={group.connection_id}
                    type="button"
                    aria-pressed={selected?.connection_id === group.connection_id}
                    onClick={() => setSelectedId(group.connection_id)}
                    className={cn(
                      "flex w-full min-w-0 items-start justify-between gap-3 px-4 py-3 text-left first:rounded-t-lg last:rounded-b-lg",
                      selected?.connection_id === group.connection_id
                        ? "bg-blue-50"
                        : "hover:bg-accent/50",
                    )}
                  >
                    <span className="min-w-0">
                      <span className="block truncate text-sm font-semibold">
                        {group.name}
                      </span>
                      <span className="mt-0.5 block text-xs text-muted-foreground">
                        capability 상한 {group.ceiling.length}개
                        {group.kind !== FIXED_KIND && ` · ${group.kind}`}
                      </span>
                    </span>
                    <StatusBadge status={group.status} />
                  </button>
                ))}
              </div>

              {selected && (
                <div className="min-w-0 space-y-5">
                  <CapSetEditForm
                    key={`${selected.connection_id}:${selected.updated_at}`}
                    group={selected}
                    onSaved={() => mutate()}
                  />
                  <CapabilityEditor key={selected.connection_id} group={selected} />
                </div>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function CapSetCreateForm({
  onCreated,
  onCancel,
}: {
  onCreated: (group: AccessConnection) => void;
  onCancel: () => void;
}) {
  const [name, setName] = useState("");
  const [ceiling, setCeiling] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const ceilingItems = parseList(ceiling);
    if (!name.trim()) {
      setError("그룹 이름을 입력해 주세요.");
      return;
    }
    if (ceilingItems.length === 0) {
      setError("capability 상한을 하나 이상 입력해 주세요.");
      return;
    }
    setBusy(true);
    setError("");
    try {
      const created = await createAccessConnection({
        name: name.trim(),
        // 서버 필수값이지만 화면에서는 묻지 않아요 — 고정값·이름 재사용.
        kind: FIXED_KIND,
        credential_mode: FIXED_CREDENTIAL_MODE,
        target: name.trim(),
        ceiling: ceilingItems,
        enforcement: "fine_grained",
      });
      onCreated(created);
    } catch (caught) {
      setError(errorMessage(caught, "권한 그룹 저장에 실패했어요."));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form
      onSubmit={submit}
      className="max-w-4xl space-y-5 rounded-lg border border-border p-4 sm:p-5"
    >
      <div>
        <h3 className="text-base font-semibold">새 권한 그룹</h3>
        <p className="mt-1 text-xs text-muted-foreground">
          그룹 ID 는 서버가 만들어요 (admin 이 정할 수 없어요).
        </p>
      </div>
      <div className="grid min-w-0 gap-4 sm:grid-cols-2">
        <Field label="그룹 이름" htmlFor="capset-name" required>
          <Input
            id="capset-name"
            value={name}
            onChange={(event) => setName(event.target.value)}
            placeholder="예: 청구서 관리"
            maxLength={120}
            required
          />
        </Field>
        <div className="sm:col-span-2">
          <Field
            label="capability 상한"
            htmlFor="capset-ceiling"
            hint="쉼표 또는 줄바꿈으로 구분"
            required
          >
            <textarea
              id="capset-ceiling"
              value={ceiling}
              onChange={(event) => setCeiling(event.target.value)}
              rows={3}
              placeholder={"invoice.read\ninvoice.approve"}
              className={textAreaClass}
              required
            />
          </Field>
          <p className="mt-1.5 text-xs text-muted-foreground">
            MCP 의 tool 이름이 아니라 admin 이 만드는 권한 이름이에요.{" "}
            <span className="font-mono text-[11px]">{"{도메인}.{동작}"}</span> 형식을
            권장해요 (예 <span className="font-mono text-[11px]">invoice.read</span>).
            소문자로 시작해야 하고, 상한이 이 그룹의 최대 허용 범위예요.
          </p>
        </div>
      </div>
      <FormActions busy={busy} error={error} onCancel={onCancel} />
    </form>
  );
}

function CapSetEditForm({
  group,
  onSaved,
}: {
  group: AccessConnection;
  onSaved: () => void;
}) {
  const [name, setName] = useState(group.name);
  const [ceiling, setCeiling] = useState(group.ceiling.join("\n"));
  const [status, setStatus] = useState(group.status);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState(false);
  const { confirm, dialog } = useConfirm();

  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const ceilingItems = parseList(ceiling);
    if (!name.trim() || ceilingItems.length === 0) {
      setError("그룹 이름과 capability 상한을 입력해 주세요.");
      return;
    }
    // 상한을 좁히면 기존 권한이 조용히 끊겨요 (판정 8번 CAPABILITY_OUTSIDE_CEILING).
    // 영향 grant 를 세어 확인 모달에 띄워요 — 조회 API 는 이미 있어서 조합으로 돼요.
    const removed = group.ceiling.filter((item) => !ceilingItems.includes(item));
    if (removed.length > 0) {
      // 조회 실패(unknown)와 0건(none)을 반드시 구분해요. 실패를 "영향 없음"으로
      // 보여주면 admin 이 수백 명의 권한을 끊으면서 안전하다고 오해해요.
      let affected: AccessGrant[] | null = null;
      try {
        affected = (await listAccessGrants()).filter(
          (grant) =>
            grant.connection_id === group.connection_id &&
            grant.status === "ACTIVE" &&
            grant.capabilities.some((cap) => removed.includes(cap)),
        );
      } catch {
        affected = null;
      }
      const impact =
        affected === null
          ? "⚠️ 영향 범위를 확인하지 못했어요 (권한 목록 조회 실패). 영향받는 권한이 있는지 알 수 없어요."
          : affected.length > 0
            ? `이 capability 를 쓰는 활성 권한 ${affected.length}건이 다음 호출부터 DENY 돼요 (판정 8번).`
            : "영향받는 활성 권한은 없어요. 다만 자산 정책이 이 capability 를 요구하면 그 tool 호출은 DENY 돼요.";
      const accepted = await confirm({
        title: "capability 상한을 좁혀요",
        description:
          `상한에서 빠지는 capability: ${removed.join(", ")}\n\n${impact}\n계속할까요?`,
        confirmLabel: "상한 좁히기",
        variant: "destructive",
      });
      if (!accepted) return;
    }
    setBusy(true);
    setError("");
    setSaved(false);
    try {
      // target 은 이 화면이 만든 그룹(kind="capability_set")일 때만 이름과 맞춰요.
      // 기존 업무 Connection(kind="aws" 등)은 target 이 ARN·DB 식별자이고, 보내면
      // 서버가 resource 를 재파생해(access_router.py:481) 데이터가 손상돼요.
      await updateAccessConnection(group.connection_id, {
        name: name.trim(),
        ...(group.kind === FIXED_KIND ? { target: name.trim() } : {}),
        ceiling: ceilingItems,
        status,
      });
      setSaved(true);
      onSaved();
    } catch (caught) {
      setError(errorMessage(caught, "권한 그룹 수정에 실패했어요."));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit} className="min-w-0 rounded-lg border border-border p-4">
      {dialog}
      <div className="mb-4 flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h3 className="text-sm font-semibold">그룹 설정</h3>
          <p className="mt-1 break-all text-xs text-muted-foreground">
            {group.connection_id}
          </p>
        </div>
        <Badge variant="outline">capability 상한 {group.ceiling.length}개</Badge>
      </div>
      {group.kind !== FIXED_KIND && (
        <div className="mb-4 rounded-lg border border-amber-200 bg-amber-50 p-3 text-xs text-amber-900">
          <b>이 레코드는 이 화면이 만든 권한 그룹이 아니에요</b> (kind=
          <span className="font-mono">{group.kind}</span>). 업무 시스템 연결로 등록된
          레코드라 target 같은 접속 정보를 갖고 있어요. 여기서는 이름·상태·상한만 고치고
          target 은 건드리지 않아요.
        </div>
      )}
      <div className="grid min-w-0 gap-3 sm:grid-cols-2">
        <Field label="그룹 이름" htmlFor={`capset-edit-name-${group.connection_id}`} required>
          <Input
            id={`capset-edit-name-${group.connection_id}`}
            value={name}
            onChange={(event) => setName(event.target.value)}
            maxLength={120}
            required
          />
        </Field>
        <Field label="상태" htmlFor={`capset-edit-status-${group.connection_id}`}>
          <Select
            id={`capset-edit-status-${group.connection_id}`}
            value={status}
            onChange={(event) => setStatus(event.target.value)}
            className="w-full"
          >
            <option value="ACTIVE">Active</option>
            <option value="DISABLED">Disabled</option>
          </Select>
        </Field>
        <div className="sm:col-span-2">
          <Field
            label="capability 상한"
            htmlFor={`capset-edit-ceiling-${group.connection_id}`}
            hint="쉼표 또는 줄바꿈으로 구분"
            required
          >
            <textarea
              id={`capset-edit-ceiling-${group.connection_id}`}
              value={ceiling}
              onChange={(event) => setCeiling(event.target.value)}
              rows={3}
              className={textAreaClass}
              required
            />
          </Field>
        </div>
      </div>
      <div className="mt-4 flex flex-wrap items-center gap-3 border-t border-border pt-4">
        <Button type="submit" size="sm" disabled={busy}>
          {busy ? "저장 중…" : "그룹 저장"}
        </Button>
        {saved && <span className="text-xs text-emerald-700">저장했어요.</span>}
        {error && <InlineError message={error} />}
      </div>
      <p className="mt-3 text-xs text-muted-foreground">
        그룹을 Disabled 로 두면 이 그룹을 요구하는 tool 호출이 모두 DENY 돼요 (판정 6번).
      </p>
    </form>
  );
}

type AccessCapabilityDraft = AccessCapability & { operationsText: string };

function CapabilityEditor({ group }: { group: AccessConnection }) {
  // 낙관적 락(결함 #9): 목록과 함께 version을 받아, 저장 시 expected_version으로 되돌려줘요.
  const {
    data,
    error: loadError,
    isLoading,
    mutate,
  } = useSWR<AccessCapabilityList>(
    accessCapabilityCacheKey(group.connection_id),
    () => listAccessCapabilityList(group.connection_id),
  );
  const [draft, setDraft] = useState<AccessCapabilityDraft[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState(false);
  const capabilities = accessCapabilityItems(data);
  const items =
    draft ??
    (capabilities ?? []).map((item) => ({
      ...item,
      operationsText: item.operations.join(", "),
    }));
  const granted = capsOf(capabilities, group.ceiling);

  function update(index: number, patch: Partial<AccessCapabilityDraft>) {
    setDraft(
      items.map((item, itemIndex) => (itemIndex === index ? { ...item, ...patch } : item)),
    );
    setSaved(false);
  }

  function add() {
    setDraft([
      ...items,
      { name: "", description: "", operations: [], operationsText: "", status: "ACTIVE" },
    ]);
    setSaved(false);
    setError("");
  }

  async function save() {
    const normalized = items.map(({ operationsText, ...item }) => ({
      ...item,
      name: item.name.trim(),
      description: item.description.trim(),
      operations: parseList(operationsText),
    }));
    if (normalized.some((item) => !item.name)) {
      setError("모든 capability 이름을 입력해 주세요.");
      return;
    }
    if (new Set(normalized.map((item) => item.name)).size !== normalized.length) {
      setError("capability 이름은 중복될 수 없어요.");
      return;
    }
    const outside = normalized.find((item) => !group.ceiling.includes(item.name));
    if (outside) {
      setError(`${outside.name}은(는) 이 그룹의 상한에 포함되지 않아요.`);
      return;
    }
    setBusy(true);
    setError("");
    setSaved(false);
    try {
      // GET에서 받은 version을 expected_version으로 되돌려요 — 그새 다른 admin이 저장했으면
      // 백엔드가 409를 돌려줘요(낙관적 락). 저장 버튼은 로딩이 끝난 뒤에만 보여서 여기서
      // data.version은 항상 정의돼 있어요.
      await putAccessCapabilities(group.connection_id, normalized, data?.version);
      setDraft(null);
      await mutate();
      setSaved(true);
    } catch (caught) {
      if (isConflict(caught)) {
        // 다른 사람이 먼저 저장 — 최신 목록을 다시 불러와 편집 내용을 버려요.
        await mutate();
        setDraft(null);
        setError(
          "다른 사람이 먼저 저장해서 목록을 다시 불러왔어요. 변경 내용을 확인하고 다시 저장해 주세요.",
        );
      } else {
        setError(errorMessage(caught, "capability 저장에 실패했어요."));
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="min-w-0 rounded-lg border border-border p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h3 className="text-sm font-semibold">capability</h3>
          <p className="mt-1 text-xs text-muted-foreground">
            상한 안에서 이 그룹이 실제로 부여하는 권한이에요.
          </p>
        </div>
        <Button
          type="button"
          variant="outline"
          size="sm"
          onClick={add}
          disabled={isLoading || Boolean(loadError)}
        >
          <Icon name="plus" size={14} />
          capability
        </Button>
      </div>

      {isLoading ? (
        <div className="mt-4">
          <LoadingRows label="capability 불러오는 중" count={2} />
        </div>
      ) : loadError ? (
        <div className="mt-4">
          <LoadError message="capability 를 불러오지 못했어요." />
        </div>
      ) : items.length === 0 ? (
        <div className="mt-4 rounded-lg border border-dashed border-border px-4 py-8 text-center text-sm text-muted-foreground">
          정의된 capability 가 없어요.
        </div>
      ) : (
        <div className="mt-4 space-y-3">
          {items.map((item, index) => (
            <div
              key={index}
              className="grid min-w-0 gap-3 border-b border-border pb-3 last:border-0 last:pb-0 sm:grid-cols-2"
            >
              <Field label="이름" htmlFor={`cap-name-${group.connection_id}-${index}`}>
                <Input
                  id={`cap-name-${group.connection_id}-${index}`}
                  value={item.name}
                  onChange={(event) => update(index, { name: event.target.value })}
                  placeholder="예: invoice.read"
                />
              </Field>
              <Field label="상태" htmlFor={`cap-status-${group.connection_id}-${index}`}>
                <Select
                  id={`cap-status-${group.connection_id}-${index}`}
                  value={item.status}
                  onChange={(event) => update(index, { status: event.target.value })}
                  className="w-full"
                >
                  <option value="ACTIVE">Active</option>
                  <option value="DISABLED">Disabled</option>
                </Select>
              </Field>
              <Field
                label="설명"
                htmlFor={`cap-description-${group.connection_id}-${index}`}
              >
                <Input
                  id={`cap-description-${group.connection_id}-${index}`}
                  value={item.description}
                  onChange={(event) => update(index, { description: event.target.value })}
                  placeholder="권한의 업무 의미"
                />
              </Field>
              <Field
                label="메모용 operation"
                htmlFor={`cap-operations-${group.connection_id}-${index}`}
                hint="쉼표로 구분 · 판정에 쓰이지 않아요"
              >
                <div className="flex min-w-0 gap-2">
                  <Input
                    id={`cap-operations-${group.connection_id}-${index}`}
                    value={item.operationsText}
                    onChange={(event) =>
                      update(index, { operationsText: event.target.value })
                    }
                    placeholder="read, list"
                    className="min-w-0"
                  />
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    onClick={() =>
                      setDraft(items.filter((_, itemIndex) => itemIndex !== index))
                    }
                    className="mt-1 shrink-0 text-red-600"
                    aria-label={`${item.name || "capability"} 삭제`}
                  >
                    삭제
                  </Button>
                </div>
              </Field>
            </div>
          ))}
        </div>
      )}

      {!loadError && !isLoading && (
        <>
          <div className="mt-4 flex flex-wrap items-center gap-3 border-t border-border pt-4">
            <Button type="button" size="sm" onClick={save} disabled={busy}>
              {busy ? "저장 중…" : "capability 저장"}
            </Button>
            {saved && <span className="text-xs text-emerald-700">저장했어요.</span>}
            {error && <InlineError message={error} />}
          </div>
          <p className="mt-3 text-xs text-muted-foreground">
            이 그룹을 배정하면 부여되는 것: {granted.length > 0 ? granted.join(", ") : "없어요"}
            {" — "}ACTIVE 이면서 상한 안인 capability 전체예요.
          </p>
          {/* `capabilitiesVersion` 은 낙관적 락을 위한 값이었어요 — 재동기화 «쓰기» 가 없어져서
              (IH-162 ③ · ADR-0112) 더 필요하지 않아요. */}
          <ResyncPanel group={group} capabilities={capabilities ?? []} />
        </>
      )}
    </section>
  );
}
