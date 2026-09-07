"use client";

/**
 * Gateway 별 Cedar 정책 콘솔.
 *
 * 왜 있나: 기존 "Cedar 정책" 화면은 원장이 아는 agent 만 대조해요. 그래서 원장 밖 정책은
 * 한 줄도 안 보였어요 — 실측에서 두 Gateway 에 11장이 ENFORCE 로 살아 있는데 화면에는
 * 없었어요. 이 화면은 Gateway 를 출발점으로 잡아요.
 *
 * 화면이 지키는 규칙 두 개:
 *  - **못 본 것을 깨끗함으로 그리지 않아요.** Target 을 못 읽으면 낡음 판정을 안 했다는
 *    사실을 그대로 띄워요(`actions_observed=false`).
 *  - **Agora 가 컴파일하는 정책의 편집은 임시예요.** 다음 배포가 덮어써요. 막지는 않지만
 *    저장 전에 그 사실을 읽게 해요.
 */

import { useMemo, useState } from "react";
import useSWR from "swr";

import { Icon } from "@/components/ui/icon";
import {
  deleteGatewayPolicy,
  getGatewayPolicies,
  policyTone,
  summarize,
  updateGatewayPolicyCedar,
  type GatewayConsoleEntry,
  type GatewayConsolePolicy,
  type GatewayPolicyReport,
  type PolicyOwnership,
  type PolicyTone,
} from "@/lib/api/gatewayPolicies";
import { cn } from "@/lib/ui";

const TONE: Record<PolicyTone, { label: string; className: string }> = {
  ok: { label: "정상", className: "border-emerald-300 bg-emerald-50 text-emerald-800" },
  stale: { label: "낡음", className: "border-red-300 bg-red-50 text-red-800" },
  unrestricted: {
    label: "action 제한 없음",
    className: "border-amber-300 bg-amber-50 text-amber-900",
  },
  unknown: { label: "관측 불가", className: "border-slate-300 bg-slate-100 text-slate-700" },
};

const OWNERSHIP: Record<PolicyOwnership, { label: string; className: string }> = {
  "agora-agent": {
    label: "Agora · agent별",
    className: "border-blue-300 bg-blue-50 text-blue-800",
  },
  "agora-shared": {
    label: "Agora · 공유",
    className: "border-indigo-300 bg-indigo-50 text-indigo-800",
  },
  "agora-domain-rule": {
    label: "Agora · 도메인 규칙",
    className: "border-violet-300 bg-violet-50 text-violet-800",
  },
  external: {
    label: "외부 · 수동",
    className: "border-slate-300 bg-slate-50 text-slate-700",
  },
};

export function GatewayPolicyConsoleClient() {
  const { data, error, isLoading, isValidating, mutate } =
    useSWR<GatewayPolicyReport>("admin/identity/gateway-policies", getGatewayPolicies, {
      revalidateOnFocus: false,
      refreshInterval: 0,
    });

  const totals = useMemo(() => {
    if (!data) return null;
    return data.gateways.reduce(
      (acc, gw) => {
        const s = summarize(gw);
        return {
          gateways: acc.gateways + 1,
          policies: acc.policies + s.total,
          stale: acc.stale + s.stale,
          unknown: acc.unknown + s.unknown,
        };
      },
      { gateways: 0, policies: 0, stale: 0, unknown: 0 },
    );
  }, [data]);

  return (
    <div className="mx-auto w-full max-w-[1400px]">
      <header className="mb-5 flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold">Gateway 정책 · Cedar</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            Gateway 마다 실제로 등록된 Cedar 정책을 그대로 읽어요. 원장에 없는 정책도
            여기서는 보여요.
          </p>
        </div>
        <button
          type="button"
          onClick={() => void mutate()}
          disabled={isValidating}
          className="inline-flex items-center gap-1.5 rounded-md border px-3 py-1.5 text-sm hover:bg-muted disabled:opacity-50"
        >
          <Icon name="refresh" className="h-4 w-4" />
          {isValidating ? "읽는 중" : "새로고침"}
        </button>
      </header>

      {totals && (
        <div className="mb-5 grid grid-cols-2 gap-3 sm:grid-cols-4">
          <Stat label="Gateway" value={totals.gateways} />
          <Stat label="정책" value={totals.policies} />
          <Stat label="낡음" value={totals.stale} tone={totals.stale ? "bad" : "plain"} />
          <Stat
            label="관측 불가"
            value={totals.unknown}
            tone={totals.unknown ? "warn" : "plain"}
          />
        </div>
      )}

      <details className="mb-5 rounded-md border bg-card px-3 py-2 text-xs">
        <summary className="cursor-pointer font-medium">배지 읽는 법</summary>
        <ul className="mt-2 space-y-1 text-muted-foreground">
          <li>
            <b>낡음</b> — 정책이 참조하는 action 이 Gateway 에 없어요. 기대값은 정책이 아니라
            Gateway 의 Target·도구 목록에서 만들어요.
          </li>
          <li>
            <b>action 제한 없음</b> — <code>action,</code> 만 써서 도구를 열거하지 않아요.
            낡을 수가 없는 대신 굵어요.
          </li>
          <li>
            <b>관측 불가</b> — 문장이나 상태를 못 읽었어요. 정상이라는 뜻이 아니에요.
          </li>
          <li>
            <b>소유자는 이름으로 추정해요.</b> <code>Agent_…_&lt;해시8&gt;_r&lt;n&gt;</code> ·
            <code>Gateway_…_r&lt;n&gt;</code> 규약에 맞으면 Agora 산출물로 봐요. 규약 이전에
            만든 Agora 정책은 &quot;외부 · 수동&quot; 으로 보여요.
          </li>
          <li>
            <b>principal</b> — 가리키는 봇이 사라졌는지는 서버가 조회하지 않아요. id 를 보고
            판단해 주세요.
          </li>
        </ul>
      </details>

      {data?.warnings.length ? (
        <ul className="mb-5 space-y-1 rounded-md border border-amber-300 bg-amber-50 p-3 text-sm text-amber-900">
          {data.warnings.map((w) => (
            <li key={w}>· {w}</li>
          ))}
        </ul>
      ) : null}

      {isLoading ? (
        <p className="rounded-md border p-6 text-sm text-muted-foreground">읽는 중이에요.</p>
      ) : error || !data ? (
        <div className="rounded-md border border-red-300 bg-red-50 p-6 text-sm text-red-800">
          <p className="font-medium">정책을 읽지 못했어요.</p>
          <p className="mt-1">
            {error instanceof Error ? error.message : "알 수 없는 오류예요."}
          </p>
        </div>
      ) : data.gateways.length === 0 ? (
        <p className="rounded-md border p-6 text-sm text-muted-foreground">
          Gateway 가 없어요.
        </p>
      ) : (
        <div className="space-y-5">
          {data.gateways.map((gw) => (
            <GatewayCard key={gw.gateway_id} entry={gw} onChanged={() => void mutate()} />
          ))}
        </div>
      )}
    </div>
  );
}

function Stat({
  label,
  value,
  tone = "plain",
}: {
  label: string;
  value: number;
  tone?: "plain" | "warn" | "bad";
}) {
  const cls =
    tone === "bad"
      ? "text-red-700"
      : tone === "warn"
        ? "text-amber-700"
        : "text-foreground";
  return (
    <div className="rounded-lg border bg-card p-3">
      <div className={cn("text-2xl font-bold tabular-nums", cls)}>{value}</div>
      <div className="mt-0.5 text-xs text-muted-foreground">{label}</div>
    </div>
  );
}

function GatewayCard({
  entry,
  onChanged,
}: {
  entry: GatewayConsoleEntry;
  onChanged: () => void;
}) {
  const summary = summarize(entry);
  const modeClass =
    entry.enforcement_mode === "ENFORCE"
      ? "border-emerald-300 bg-emerald-50 text-emerald-800"
      : entry.enforcement_mode === "LOG_ONLY"
        ? "border-amber-300 bg-amber-50 text-amber-900"
        : "border-slate-300 bg-slate-100 text-slate-700";

  return (
    <section className="rounded-lg border bg-card">
      <header className="flex flex-wrap items-start justify-between gap-3 border-b px-4 py-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <h2 className="text-lg font-semibold">{entry.name}</h2>
            <span className={cn("rounded-full border px-2 py-0.5 text-xs font-medium", modeClass)}>
              {entry.enforcement_mode || "모드 관측 불가"}
            </span>
            <span className="text-xs text-muted-foreground">정책 {summary.total}장</span>
            {summary.stale > 0 && (
              <span className="rounded-full border border-red-300 bg-red-50 px-2 py-0.5 text-xs font-medium text-red-800">
                낡음 {summary.stale}
              </span>
            )}
          </div>
          <p className="mt-1 truncate font-mono text-xs text-muted-foreground">
            engine {entry.engine_id || "(없음)"}
          </p>
          {entry.target_names.length > 0 && (
            <p className="mt-1 font-mono text-xs text-muted-foreground">
              Target {entry.target_names.join(" · ")}
            </p>
          )}
        </div>
      </header>

      {!entry.actions_observed && (
        <p className="border-b bg-slate-50 px-4 py-2 text-xs text-slate-700">
          Gateway Target 을 읽지 못해 <b>낡음 판정을 하지 않았어요.</b> 아래 정책이 깨끗하다는
          뜻이 아니에요.
        </p>
      )}
      {/* 엔진이 없다는 사실은 아래 빈 목록 문구가 이미 말해요. 같은 문장을 두 번 띄우면
          읽는 사람이 서로 다른 두 문제로 착각해요(2026-08-29 브라우저 확인에서 잡혔어요). */}
      {entry.reason && entry.engine_id && (
        <p className="border-b bg-amber-50 px-4 py-2 text-xs text-amber-900">{entry.reason}</p>
      )}

      {entry.policies.length === 0 ? (
        <p className="px-4 py-5 text-sm text-muted-foreground">
          {entry.engine_id
            ? entry.reason || "이 엔진에 등록된 정책이 없어요."
            : "policy engine 이 붙어 있지 않아요. Cedar 판정 없이 인증만으로 통과해요."}
        </p>
      ) : (
        <ul className="divide-y">
          {entry.policies.map((policy) => (
            <PolicyRow
              key={policy.policy_id}
              engineId={entry.engine_id}
              policy={policy}
              actionsObserved={entry.actions_observed}
              onChanged={onChanged}
            />
          ))}
        </ul>
      )}
    </section>
  );
}

function PolicyRow({
  engineId,
  policy,
  actionsObserved,
  onChanged,
}: {
  engineId: string;
  policy: GatewayConsolePolicy;
  actionsObserved: boolean;
  onChanged: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(policy.cedar);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [busy, setBusy] = useState("");
  const [message, setMessage] = useState("");
  const [failed, setFailed] = useState(false);

  const tone = policyTone(policy, actionsObserved);
  const owner = OWNERSHIP[policy.ownership];

  async function save() {
    setBusy("save");
    setMessage("");
    setFailed(false);
    try {
      const result = await updateGatewayPolicyCedar(engineId, policy.policy_id, draft);
      setFailed(!result.ok);
      setMessage(
        result.ok
          ? `저장했어요. 상태 ${result.status}.`
          : `반영되지 않았어요 (${result.status}). ${result.reason}`,
      );
      if (result.ok) setEditing(false);
      onChanged();
    } catch (e) {
      setFailed(true);
      setMessage(e instanceof Error ? e.message : "저장하지 못했어요.");
    } finally {
      setBusy("");
    }
  }

  async function remove() {
    setBusy("delete");
    setMessage("");
    setFailed(false);
    try {
      const result = await deleteGatewayPolicy(engineId, policy.policy_id);
      setFailed(!result.absence_confirmed);
      setMessage(
        result.absence_confirmed
          ? "삭제했어요. 목록에서 사라진 것까지 확인했어요."
          : result.reason,
      );
      setConfirmDelete(false);
      onChanged();
    } catch (e) {
      setFailed(true);
      setMessage(e instanceof Error ? e.message : "삭제하지 못했어요.");
    } finally {
      setBusy("");
    }
  }

  return (
    <li className="px-4 py-3">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <button
              type="button"
              onClick={() => setOpen((v) => !v)}
              className="inline-flex items-center gap-1 font-mono text-sm font-medium hover:underline"
            >
              {/* ICON_PATHS 에 chevron 키가 없어요(nav.ts:125). 문자로 두면 키가 늘거나
                  줄어도 안 깨져요. */}
              <span aria-hidden className="inline-block w-3 text-muted-foreground">
                {open ? "▾" : "▸"}
              </span>
              {policy.name || policy.policy_id}
            </button>
            <span className={cn("rounded-full border px-2 py-0.5 text-xs font-medium", TONE[tone].className)}>
              {TONE[tone].label}
            </span>
            <span className={cn("rounded-full border px-2 py-0.5 text-xs", owner.className)}>
              {owner.label}
            </span>
            {policy.status && policy.status !== "ACTIVE" && (
              <span className="rounded-full border border-slate-300 bg-slate-100 px-2 py-0.5 text-xs text-slate-700">
                {policy.status}
              </span>
            )}
          </div>
          <p className="mt-1 text-xs text-muted-foreground">
            {policy.action_unrestricted
              ? "action 을 제한하지 않아요 (굵은 문)"
              : `action ${policy.actions.length}개`}
            {" · "}
            {policy.size_bytes} B
            {policy.created_at ? ` · ${policy.created_at.slice(0, 19).replace("T", " ")}` : ""}
          </p>
          {/* principal 은 낡음의 두 번째 축이에요 — action 은 멀쩡한데 가리키는 봇이
              사라진 정책이 실제로 있어요(옛 PoC). id 를 띄워 관리자가 판단하게 해요.
              존재 여부를 서버가 조회하지는 않아요. */}
          <p className="mt-1 truncate font-mono text-xs text-muted-foreground">
            principal {policy.principal_type || "?"}
            {" · "}
            {policy.principal_ids.length > 0
              ? policy.principal_ids.join(", ")
              : "개체 지목 없음 (타입 전체)"}
          </p>
          {policy.stale_actions.length > 0 && (
            <p className="mt-1 rounded border border-red-300 bg-red-50 px-2 py-1 font-mono text-xs text-red-800">
              Gateway 에 없는 action: {policy.stale_actions.join(", ")}
            </p>
          )}
          {policy.read_error && (
            <p className="mt-1 text-xs text-amber-800">읽기 실패: {policy.read_error}</p>
          )}
          {policy.status_reasons.length > 0 && (
            <p className="mt-1 text-xs text-amber-800">{policy.status_reasons.join("; ")}</p>
          )}
        </div>

        <div className="flex shrink-0 items-center gap-2">
          <button
            type="button"
            onClick={() => {
              setDraft(policy.cedar);
              setEditing(true);
              setOpen(true);
              setMessage("");
            }}
            disabled={!engineId || !!busy || !!policy.read_error}
            className="rounded-md border px-2.5 py-1 text-xs hover:bg-muted disabled:opacity-50"
          >
            Cedar 수정
          </button>
          <button
            type="button"
            onClick={() => setConfirmDelete(true)}
            disabled={!engineId || !!busy}
            className="rounded-md border border-red-300 px-2.5 py-1 text-xs text-red-700 hover:bg-red-50 disabled:opacity-50"
          >
            삭제
          </button>
        </div>
      </div>

      {message && (
        <p
          className={cn(
            "mt-2 rounded border px-2 py-1 text-xs",
            failed
              ? "border-red-300 bg-red-50 text-red-800"
              : "border-emerald-300 bg-emerald-50 text-emerald-800",
          )}
        >
          {message}
        </p>
      )}

      {confirmDelete && (
        <div className="mt-2 rounded-md border border-red-300 bg-red-50 p-3 text-xs text-red-900">
          <p className="font-medium">이 정책을 지울까요?</p>
          <p className="mt-1">
            삭제는 즉시 반영되지 않아요. 0.8~3.1초 동안 계속 허용될 수 있어요.
            {policy.ownership !== "external" && (
              <>
                {" "}
                이 정책은 <b>Agora 가 원장에서 컴파일한 것</b>이라, 해당 agent 를 다시
                배포하면 되살아나요.
              </>
            )}
          </p>
          <div className="mt-2 flex gap-2">
            <button
              type="button"
              onClick={() => void remove()}
              disabled={busy === "delete"}
              className="rounded-md bg-red-700 px-3 py-1 font-medium text-white hover:bg-red-800 disabled:opacity-50"
            >
              {busy === "delete" ? "지우는 중" : "지워요"}
            </button>
            <button
              type="button"
              onClick={() => setConfirmDelete(false)}
              className="rounded-md border border-red-300 px-3 py-1"
            >
              그만
            </button>
          </div>
        </div>
      )}

      {open && (
        <div className="mt-2">
          {editing ? (
            <>
              {policy.edit_overwritten_by_deploy && (
                <p className="mb-2 rounded border border-amber-300 bg-amber-50 px-2 py-1.5 text-xs text-amber-900">
                  이 정책은 원장에서 컴파일돼요. 손으로 고치면 <b>다음 배포가 덮어써요</b>,
                  그 사이에는 원장과 라이브가 어긋나요. 영구 변경은 원장(Agent × Tool)에서
                  해야 해요.
                </p>
              )}
              <textarea
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
                spellCheck={false}
                rows={Math.min(24, Math.max(8, draft.split("\n").length + 2))}
                className="w-full rounded-md border bg-background p-3 font-mono text-xs leading-relaxed"
              />
              <p className="mt-1 text-xs text-muted-foreground">
                {new TextEncoder().encode(draft).length} B / 10,000 B · 검증은 비동기예요.
                저장 호출이 성공해도 종료 상태를 확인할 때까지 통과가 아니에요.
              </p>
              <div className="mt-2 flex gap-2">
                <button
                  type="button"
                  onClick={() => void save()}
                  disabled={busy === "save" || !draft.trim() || draft === policy.cedar}
                  className="rounded-md bg-foreground px-3 py-1 text-xs font-medium text-background disabled:opacity-50"
                >
                  {busy === "save" ? "검증 중" : "저장하고 검증"}
                </button>
                <button
                  type="button"
                  onClick={() => {
                    setEditing(false);
                    setDraft(policy.cedar);
                  }}
                  className="rounded-md border px-3 py-1 text-xs"
                >
                  그만
                </button>
              </div>
            </>
          ) : (
            <pre className="overflow-x-auto rounded-md border bg-muted/40 p-3 font-mono text-xs leading-relaxed">
              {policy.cedar || "(문장을 읽지 못했어요)"}
            </pre>
          )}
        </div>
      )}
    </li>
  );
}
