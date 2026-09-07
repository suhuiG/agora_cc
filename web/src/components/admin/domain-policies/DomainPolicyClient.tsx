"use client";

// 도메인 규칙 Cedar 정책 콘솔 (IH-132).
//
// 이 화면이 만드는 건 「환불 금액이 100,000원을 넘으면 안 된다」 같은 **값 조건**이에요.
// 「누가 무엇을 부를 수 있나」는 여기서 만들지 않아요 — 그건 REQUEST interceptor 가 원장을
// 읽어서 판정해요(ADR-0091). Cedar 만 볼 수 있는 것이 요청 본문의 인자 값이라, Cedar 에는
// 도메인 규칙만 남겨요(docs/design/tool-authn-authz-final.html §10).
//
// 화면이 반드시 말해야 하는 것 셋이에요. 어느 것도 접어두지 않아요.
//   ① 변수명이 1글자만 달라도 정책이 조용히 무력화돼요 (`ARGUMENT_NAME_WARNING`).
//   ② 굵은 문이 그 도구를 이미 허용하면 이 정책은 장식이에요.
//   ③ LOG_ONLY 의 뜻은 굵은 문 상태에 따라 뒤집혀요 (`EFFECT_COPY`).

import { useMemo, useState } from "react";
import useSWR from "swr";

import {
  ARGUMENT_NAME_WARNING,
  EFFECT_COPY,
  type AssetOption,
  type CoarseConflict,
  type DomainPolicyList,
  type DomainPolicyOptions,
  type DomainPolicyPreview,
  type DomainPolicyRule,
  type DomainRuleDraft,
  type ToolOption,
  coarsePermitsFromRule,
  createDomainPolicy,
  deleteDomainPolicy,
  enforcementEffect,
  getDomainPolicies,
  getDomainPolicyOptions,
  needsFreeTextArgument,
  previewDomainPolicy,
  promoteDomainPolicy,
  refreshDomainPolicy,
  ruleExpression,
  selectableArguments,
} from "@/lib/api/domainPolicies";
import {
  GATEWAY_FIELD_LABEL,
  GATEWAY_HELP,
  GATEWAY_HELP_LABEL,
  GATEWAY_SINGLE_CHOICE_HINT,
  GATEWAY_UNRESOLVED_LABEL,
  PERMIT_SHAPE_HELP,
  PERMIT_SHAPE_HELP_LABEL,
  WHY_PERMIT_HELP,
  WHY_PERMIT_HELP_LABEL,
  WHY_PERMIT_QUESTION,
  gatewayDisplayName,
} from "@/lib/domainPolicyCopy";
import { ApiError } from "@/lib/api/client";
import { cn } from "@/lib/ui";
import {
  EmptyState,
  Field,
  FormActions,
  LoadError,
  LoadingRows,
  PageHeading,
  errorMessage,
  textAreaClass,
} from "@/components/admin/access/shared";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { useConfirm } from "@/components/ui/confirm-dialog";
import { HelpPopover } from "@/components/ui/help-popover";
import { Icon } from "@/components/ui/icon";
import { Input, Select } from "@/components/ui/input";
import { useToast } from "@/components/ui/toast";
import { CedarExamplesButton } from "./CedarExamplesButton";

const EMPTY_DRAFT: DomainRuleDraft = {
  asset_id: "",
  asset_version: "",
  target_name: "",
  tool_name: "",
  argument: "",
  operator: "<=",
  value_kind: "number",
  value: "",
  description: "",
};

const TONE_CLASS: Record<string, string> = {
  info: "border-blue-200 bg-blue-50 text-blue-900",
  warn: "border-amber-300 bg-amber-50 text-amber-900",
  ok: "border-emerald-300 bg-emerald-50 text-emerald-900",
  muted: "border-slate-300 bg-slate-100 text-slate-700",
};

const MODE_CLASS: Record<string, string> = {
  LOG_ONLY: "border-amber-300 bg-amber-50 text-amber-900",
  ACTIVE: "border-emerald-300 bg-emerald-50 text-emerald-800",
  "": "border-slate-300 bg-slate-100 text-slate-700",
};

function modeLabel(mode: string): string {
  if (mode === "LOG_ONLY") return "LOG_ONLY · 관측만";
  if (mode === "ACTIVE") return "ACTIVE · 강제";
  return "모드 미관측";
}

/** 팝오버 본문 — 상수 배열을 문단으로 그려요. 리터럴을 여기 쓰지 않아요. */
function HelpParagraphs({ lines }: { lines: readonly string[] }) {
  return (
    <>
      {lines.map((line) => (
        <p key={line} className="mt-2 first:mt-0">
          {line}
        </p>
      ))}
    </>
  );
}

/**
 * 대상 Gateway — **보이는 선택기, 그러나 고를 수 있는 건 하나**예요.
 *
 * 서버가 좌표를 설정에서 도출하고 요청 본문에 gateway 필드가 없어요. 그래서 선택기에는
 * 서버가 준 그 하나만 들어가고 `disabled` 예요. 못 고르는 이유는 옆 `?` 에 있어요.
 *
 * ⚠️ 목록을 하드코딩하지 않아요. 다른 Gateway 가 있는지는 이 화면이 관측하지 못해요 —
 * 관측하지 않은 목록을 화면이 주장하면 안 돼요.
 */
function GatewayChoice({ arn }: { arn: string }) {
  const name = gatewayDisplayName(arn);
  return (
    <div className="min-w-0">
      <span className="mb-1 flex flex-wrap items-center gap-x-1 text-xs font-medium">
        {GATEWAY_FIELD_LABEL}
        <span className="font-normal text-muted-foreground">
          · {GATEWAY_SINGLE_CHOICE_HINT}
        </span>
        <HelpPopover label={GATEWAY_HELP_LABEL}>
          <HelpParagraphs lines={GATEWAY_HELP} />
        </HelpPopover>
      </span>
      {/* 「고를 수 있는 건 하나」를 형태로 말해요. 값을 바꿀 수 없으니 `disabled` 예요. */}
      <Select
        id="domain-policy-gateway"
        // 비활성인 게 보여야 해요 — 겉모습이 고를 수 있는 것처럼 보이면 눌러 보고 헤매요.
        className="h-9 max-w-full disabled:cursor-not-allowed disabled:bg-muted/50 disabled:text-muted-foreground"
        aria-label={GATEWAY_FIELD_LABEL}
        value={arn}
        disabled
        title={arn || GATEWAY_UNRESOLVED_LABEL}
      >
        <option value={arn}>{name || GATEWAY_UNRESOLVED_LABEL}</option>
      </Select>
      {/* ARN 원문은 지우지 않아요 — 운영자가 라이브와 대조해야 해요. */}
      <p
        className="mt-1 max-w-full truncate font-mono text-[11px] text-muted-foreground"
        title={arn || GATEWAY_UNRESOLVED_LABEL}
      >
        {arn || GATEWAY_UNRESOLVED_LABEL}
      </p>
    </div>
  );
}

/** 굵은 문 충돌을 눈에 띄게 보여줘요. 접히지 않는 배너예요. */
function ConflictBanner({
  observed,
  reason,
  conflicts,
}: {
  observed: boolean;
  reason: string;
  conflicts: CoarseConflict[];
}) {
  if (!observed) {
    return (
      <div className="rounded-lg border border-slate-300 bg-slate-100 p-3 text-xs text-slate-700">
        <b className="block text-[13px]">굵은 문 충돌을 확인하지 못했어요</b>
        <p className="mt-1">{reason}</p>
        <p className="mt-1">
          관측하지 못한 것을 「충돌 없음」으로 읽지 않아요. 이 상태로는 저장이 막혀요.
        </p>
      </div>
    );
  }
  if (conflicts.length === 0) {
    return (
      <div className="rounded-lg border border-emerald-300 bg-emerald-50 p-3 text-xs text-emerald-900">
        <b className="block text-[13px]">이 도구를 허용하는 다른 permit 이 없어요</b>
        <p className="mt-1">
          ACTIVE 로 승격하면 임계값이 실제로 강제돼요. 승격 전에는 이 도구 호출이 전부
          거부돼요(Cedar 는 기본이 거부예요).
        </p>
      </div>
    );
  }
  return (
    <div className="rounded-lg border border-amber-300 bg-amber-50 p-3 text-xs text-amber-900">
      <b className="block text-[13px]">
        굵은 문이 이 도구를 이미 허용해요 — 이 정책은 지금 효력이 없어요
      </b>
      <p className="mt-1">
        Cedar 의 <code className="font-mono">permit</code> 은 합집합이에요. 좁은 정책을
        추가해도 권한이 좁아지지 않아요. 실제로 막으려면 아래 정책에서 이 도구를 먼저 빼야
        해요.
      </p>
      <ul className="mt-2 space-y-1">
        {conflicts.map((conflict) => (
          <li key={`${conflict.policy_id}:${conflict.kind}`}>
            · <span className="font-mono">{conflict.policy_name || conflict.policy_id}</span>{" "}
            — {conflict.label}
          </li>
        ))}
      </ul>
    </div>
  );
}

function CreateForm({
  options,
  onCreated,
  onCancel,
}: {
  options: DomainPolicyOptions;
  onCreated: () => Promise<void>;
  onCancel: () => void;
}) {
  const [draft, setDraft] = useState<DomainRuleDraft>(EMPTY_DRAFT);
  const [preview, setPreview] = useState<DomainPolicyPreview | null>(null);
  const [acknowledge, setAcknowledge] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const { success, error: toastError } = useToast();

  const asset: AssetOption | undefined = useMemo(
    () => options.assets.find((item) => item.asset_id === draft.asset_id),
    [options.assets, draft.asset_id],
  );
  const tool: ToolOption | undefined = useMemo(
    () => asset?.tools.find((item) => item.tool_name === draft.tool_name),
    [asset, draft.tool_name],
  );
  const choices = selectableArguments(tool);
  const freeText = needsFreeTextArgument(tool);
  const operators =
    draft.value_kind === "string"
      ? options.string_operators
      : options.numeric_operators;

  function patch(next: Partial<DomainRuleDraft>) {
    // 값이 바뀌면 미리보기는 낡아요 — 낡은 문장을 보고 저장하면 다른 걸 승인한 셈이에요.
    setPreview(null);
    setAcknowledge(false);
    setError("");
    setDraft((current) => ({ ...current, ...next }));
  }

  const ready =
    Boolean(draft.asset_id && draft.tool_name && draft.argument && draft.value);

  async function runPreview() {
    setBusy(true);
    setError("");
    try {
      setPreview(await previewDomainPolicy(draft));
    } catch (caught) {
      setPreview(null);
      setError(errorMessage(caught, "미리보기를 만들지 못했어요."));
    } finally {
      setBusy(false);
    }
  }

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (!preview) {
      setError("저장하기 전에 미리보기로 Cedar 문장을 확인해 주세요.");
      return;
    }
    setBusy(true);
    setError("");
    try {
      await createDomainPolicy(draft, acknowledge);
      success("도메인 규칙을 만들었어요", "LOG_ONLY 로 만들었어요 — 강제는 승격해야 켜져요.");
      await onCreated();
    } catch (caught) {
      const detail = caught instanceof ApiError ? caught.detail : undefined;
      const message =
        (detail as { message?: string } | undefined)?.message ??
        errorMessage(caught, "정책을 만들지 못했어요.");
      setError(message);
      toastError("저장하지 못했어요", message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card>
      <CardContent className="p-5">
        <form onSubmit={submit} className="space-y-4">
          {/* ① 변수명 경고 — 항상 보여요. 접지 않아요. */}
          <div className="rounded-lg border border-red-200 bg-red-50 p-3.5 text-xs text-red-900">
            <b className="block text-[13px]">
              {ARGUMENT_NAME_WARNING}
            </b>
            <p className="mt-1.5">
              오타, 대소문자 차이(<code className="font-mono">amount</code> ≠{" "}
              <code className="font-mono">Amount</code>), 도구가 그 인자를 아예 안 받는 경우 —
              모두 <b>정책이 조용히 무력화되고 호출은 그대로 통과</b>해요. 거부되는 게 아니라
              규칙이 없는 것처럼 돼요.
            </p>
            <p className="mt-1.5">
              그래서 아래에서 <b>도구가 선언한 인자를 골라 주세요.</b> 목록에 없는 이름을 직접
              적는 건 스키마를 읽지 못했을 때만 쓰세요.
            </p>
          </div>

          <Field label="자산" htmlFor="asset" required hint="배포된 MCP 자산이에요.">
            <Select
              id="asset"
              value={draft.asset_id}
              onChange={(event) => {
                const picked = options.assets.find(
                  (item) => item.asset_id === event.target.value,
                );
                patch({
                  asset_id: event.target.value,
                  asset_version: picked?.asset_version ?? "",
                  tool_name: "",
                  target_name: "",
                  argument: "",
                });
              }}
            >
              <option value="">선택해 주세요</option>
              {options.assets.map((item) => (
                <option key={item.asset_id} value={item.asset_id} disabled={!item.tools.length}>
                  {item.name}
                  {item.tools.length ? "" : " (도구 없음)"}
                </option>
              ))}
            </Select>
          </Field>
          {asset?.reason ? (
            <p className="rounded border border-amber-300 bg-amber-50 px-2 py-1 text-xs text-amber-900">
              {asset.reason}
            </p>
          ) : null}

          <Field label="도구" htmlFor="tool" required hint="Cedar action 이 되는 도구예요.">
            <Select
              id="tool"
              value={draft.tool_name}
              disabled={!asset}
              onChange={(event) => {
                const picked = asset?.tools.find(
                  (item) => item.tool_name === event.target.value,
                );
                patch({
                  tool_name: event.target.value,
                  target_name: picked?.target_name ?? "",
                  argument: "",
                });
              }}
            >
              <option value="">선택해 주세요</option>
              {(asset?.tools ?? []).map((item) => (
                <option key={item.tool_name} value={item.tool_name}>
                  {item.tool_name}
                  {item.sensitivity ? ` · ${item.sensitivity}` : ""}
                </option>
              ))}
            </Select>
          </Field>
          {tool && !tool.schema_observed ? (
            <p className="rounded border border-red-300 bg-red-50 px-2.5 py-2 text-xs text-red-900">
              <b>이 도구의 inputSchema 를 읽지 못했어요</b> ({tool.schema_reason}). 인자 이름을
              확인할 수 없으니 직접 적어야 해요 — 위 경고가 그만큼 더 위험해요. 자산 등록
              정보를 고친 뒤 다시 시도하는 편이 안전해요.
            </p>
          ) : null}

          <Field
            label="인자 이름"
            htmlFor="argument"
            required
            hint={
              freeText
                ? "스키마를 읽지 못해 자유 입력이에요. 도구 선언과 정확히 같게 적어 주세요."
                : "도구가 선언한 인자 중에서 고르세요."
            }
          >
            {freeText ? (
              <Input
                id="argument"
                value={draft.argument}
                placeholder="amount"
                onChange={(event) => patch({ argument: event.target.value })}
              />
            ) : (
              <Select
                id="argument"
                value={draft.argument}
                onChange={(event) => patch({ argument: event.target.value })}
              >
                <option value="">선택해 주세요</option>
                {choices.map((item) => (
                  <option key={item.name} value={item.name}>
                    {item.name}
                    {item.json_type ? ` · ${item.json_type}` : ""}
                    {item.required ? " · 필수" : ""}
                  </option>
                ))}
              </Select>
            )}
          </Field>
          {/* 고를 수 없는 인자는 **이유를 각각** 보여줘요. 하나로 묶어 「식별자가 아니에요」로
              적으면, 예약 인자(`agora_user_id`)처럼 다른 이유로 막힌 것에 틀린 설명이 붙어요. */}
          {tool?.arguments.some((argument) => !argument.usable) ? (
            <ul className="space-y-0.5 rounded border border-slate-300 bg-slate-100 px-2.5 py-2 text-xs text-slate-700">
              <li className="font-medium">고를 수 없는 인자도 있어요</li>
              {tool.arguments
                .filter((argument) => !argument.usable)
                .map((argument) => (
                  <li key={argument.name}>
                    · <span className="font-mono">{argument.name}</span> — {argument.reason}
                  </li>
                ))}
            </ul>
          ) : null}

          <div className="grid gap-4 sm:grid-cols-3">
            <Field label="값 종류" htmlFor="value_kind" required>
              <Select
                id="value_kind"
                value={draft.value_kind}
                onChange={(event) => {
                  const kind = event.target.value as "number" | "string";
                  patch({ value_kind: kind, operator: kind === "string" ? "==" : "<=" });
                }}
              >
                <option value="number">숫자 (정수)</option>
                <option value="string">문자열</option>
              </Select>
            </Field>
            <Field label="연산자" htmlFor="operator" required>
              <Select
                id="operator"
                value={draft.operator}
                onChange={(event) => patch({ operator: event.target.value })}
              >
                {operators.map((operator) => (
                  <option key={operator} value={operator}>
                    {operator}
                  </option>
                ))}
              </Select>
            </Field>
            <Field
              label="임계값"
              htmlFor="value"
              required
              hint={draft.value_kind === "number" ? "정수만 써요." : ""}
            >
              <Input
                id="value"
                value={draft.value}
                placeholder={draft.value_kind === "number" ? "100000" : "KRW"}
                onChange={(event) => patch({ value: event.target.value })}
              />
            </Field>
          </div>

          <Field label="설명" htmlFor="description" hint="왜 이 규칙이 필요한지 적어 주세요.">
            <textarea
              id="description"
              className={textAreaClass}
              rows={2}
              value={draft.description}
              onChange={(event) => patch({ description: event.target.value })}
            />
          </Field>

          <div className="flex flex-wrap items-center gap-2 border-t border-border pt-4">
            <Button type="button" variant="outline" disabled={!ready || busy} onClick={runPreview}>
              <Icon name="eye" size={14} />
              미리보기
            </Button>
            <span className="text-xs text-muted-foreground">
              Cedar 문장은 서버가 조립해요. 저장 전에 그 문장을 그대로 확인해 주세요.
            </span>
          </div>

          {/* ② 미리보기 — 서버가 조립할 문장 그대로 */}
          {preview ? (
            <div className="space-y-3">
              <div>
                <p className="mb-1.5 text-xs font-medium text-muted-foreground">
                  서버가 만들 Cedar 문장 ({preview.size_bytes} 바이트)
                </p>
                <pre className="overflow-x-auto rounded-md border bg-muted/40 p-3 font-mono text-xs leading-relaxed">
                  {preview.cedar}
                </pre>
                <p className="mt-1.5 text-xs text-muted-foreground">
                  <code className="font-mono">context.input has {draft.argument}</code> 가드가
                  먼저 와요 — 인자가 없으면 이 문장이 안 맞고, 맞는 permit 이 없으면 Cedar 는
                  기본 거부예요. <code className="font-mono">forbid</code> 로 쓰면 인자를 빼는
                  것만으로 규칙이 사라져요.
                </p>
              </div>

              <ConflictBanner
                observed={preview.conflict.observed}
                reason={preview.conflict.reason}
                conflicts={preview.conflict.conflicts}
              />

              <div className="rounded-lg border border-amber-300 bg-amber-50 p-3 text-xs text-amber-900">
                <b className="block text-[13px]">만들면 LOG_ONLY 로 시작해요</b>
                <p className="mt-1">
                  임계값을 잘못 잡으면 정상 업무가 막혀요. 그래서 강제(ACTIVE)는 목록에서
                  사람이 직접 승격해요. 승격할 때 굵은 문 충돌을 다시 확인해요.
                </p>
              </div>

              {preview.conflict.observed && preview.conflict.conflicts.length > 0 ? (
                <label className="flex items-start gap-2 rounded-md border border-amber-400 bg-amber-100/60 p-3 text-xs text-amber-950">
                  <input
                    type="checkbox"
                    className="mt-0.5"
                    checked={acknowledge}
                    onChange={(event) => setAcknowledge(event.target.checked)}
                  />
                  <span>
                    이 정책이 <b>지금은 효력이 없다</b>는 것을 알고도 저장해요. (굵은 문에서 이
                    도구를 뺄 때까지 요청은 거부되지 않아요. 이 사실이 원장에 남아요.)
                  </span>
                </label>
              ) : null}
            </div>
          ) : null}

          <FormActions
            busy={busy}
            error={error}
            onCancel={onCancel}
            submitLabel="LOG_ONLY 로 만들기"
          />
        </form>
      </CardContent>
    </Card>
  );
}

function RuleCard({
  rule,
  onChanged,
}: {
  rule: DomainPolicyRule;
  onChanged: () => Promise<void>;
}) {
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [failed, setFailed] = useState(false);
  const { confirm, dialog } = useConfirm();
  const { success, error: toastError } = useToast();

  const coarsePermits = coarsePermitsFromRule(rule);
  const effect = enforcementEffect(
    rule.enforcement_mode,
    rule.enforcement_mode ? coarsePermits : null,
  );
  const copy = EFFECT_COPY[effect];

  async function act(
    run: () => Promise<unknown>,
    okMessage: string,
  ) {
    setBusy(true);
    setMessage("");
    setFailed(false);
    try {
      await run();
      setMessage(okMessage);
      success(okMessage);
      await onChanged();
    } catch (caught) {
      const detail = caught instanceof ApiError ? caught.detail : undefined;
      const text =
        (detail as { message?: string } | undefined)?.message ??
        errorMessage(caught, "요청이 실패했어요.");
      setMessage(text);
      setFailed(true);
      toastError("실패했어요", text);
    } finally {
      setBusy(false);
    }
  }

  async function promote() {
    const acknowledged = coarsePermits
      ? await confirm({
          title: "효력이 없는 상태로 승격할까요?",
          description:
            "굵은 문이 이 도구를 이미 허용하고 있어요. ACTIVE 로 올려도 요청은 거부되지 않아요.",
          confirmLabel: "알고도 승격",
          variant: "destructive",
        })
      : await confirm({
          title: "강제를 켤까요?",
          description:
            "ACTIVE 로 올리면 임계값을 만족하지 않는 호출이 거부돼요. 임계값을 다시 확인해 주세요.",
          confirmLabel: "ACTIVE 로 승격",
        });
    if (!acknowledged) return;
    await act(
      () => promoteDomainPolicy(rule.rule_id, coarsePermits),
      "ACTIVE 로 승격했어요.",
    );
  }

  async function remove() {
    const ok = await confirm({
      title: "이 도메인 규칙을 지울까요?",
      description: "원격 Cedar 정책과 원장 행을 함께 지워요.",
      confirmLabel: "삭제",
      variant: "destructive",
    });
    if (!ok) return;
    await act(() => deleteDomainPolicy(rule.rule_id), "지웠어요.");
  }

  return (
    <Card>
      <CardContent className="space-y-3 p-4">
        <div className="flex flex-wrap items-start justify-between gap-2">
          <div className="min-w-0">
            <p className="font-mono text-sm font-medium text-foreground">
              {rule.gateway_action}
            </p>
            <p className="mt-0.5 font-mono text-xs text-muted-foreground">
              {ruleExpression(rule)}
            </p>
          </div>
          <div className="flex flex-wrap items-center gap-1.5">
            <span
              className={cn(
                "rounded-full border px-2 py-0.5 text-[11px] font-medium",
                MODE_CLASS[rule.enforcement_mode] ?? MODE_CLASS[""],
              )}
            >
              {modeLabel(rule.enforcement_mode)}
            </span>
            <span
              className={cn(
                "rounded-full border px-2 py-0.5 text-[11px]",
                rule.observed_status === "ACTIVE"
                  ? "border-emerald-300 bg-emerald-50 text-emerald-800"
                  : "border-slate-300 bg-slate-100 text-slate-700",
              )}
            >
              검증 {rule.observed_status || "미관측"}
            </span>
          </div>
        </div>

        <div
          className={cn(
            "rounded-md border p-2.5 text-xs",
            TONE_CLASS[copy.tone] ?? TONE_CLASS.muted,
          )}
        >
          <b className="block text-[12.5px]">{copy.headline}</b>
          <p className="mt-1">{copy.body}</p>
        </div>

        {rule.description ? (
          <p className="text-xs text-muted-foreground">{rule.description}</p>
        ) : null}

        {rule.coarse_conflict_policies.length ? (
          <p className="rounded border border-amber-300 bg-amber-50 px-2 py-1 text-xs text-amber-900">
            그때 관측한 충돌:{" "}
            <span className="font-mono">{rule.coarse_conflict_policies.join(", ")}</span>
            {rule.conflict_acknowledged ? " (관리자가 알고도 저장했어요)" : ""}
          </p>
        ) : null}

        {rule.status_reasons.length ? (
          <ul className="space-y-0.5 rounded border border-slate-300 bg-slate-100 px-2 py-1 text-xs text-slate-700">
            {rule.status_reasons.map((reason) => (
              <li key={reason}>· {reason}</li>
            ))}
          </ul>
        ) : null}

        <details className="rounded-md border bg-card px-3 py-2 text-xs">
          <summary className="cursor-pointer font-medium">Cedar 문장과 이력</summary>
          <pre className="mt-2 overflow-x-auto rounded-md border bg-muted/40 p-3 font-mono text-xs leading-relaxed">
            {rule.cedar_policy}
          </pre>
          <p className="mt-2 text-muted-foreground">
            정책 <span className="font-mono">{rule.remote_policy_name}</span> ·{" "}
            {rule.created_by} 님이 {rule.created_at} 에 만들었어요.
          </p>
          <ul className="mt-1.5 space-y-0.5 text-muted-foreground">
            {rule.enforcement_changes.map((change, index) => (
              <li key={`${change.changed_at}:${index}`}>
                · {change.reason || change.requested_mode} — 요청 {change.requested_mode} /
                관측 {change.observed_mode || "미관측"} · {change.changed_by} ·{" "}
                {change.changed_at}
              </li>
            ))}
          </ul>
        </details>

        <div className="flex flex-wrap items-center gap-2">
          {rule.enforcement_mode === "ACTIVE" ? null : (
            <Button size="sm" disabled={busy} onClick={promote}>
              <Icon name="check" size={14} />
              ACTIVE 로 승격
            </Button>
          )}
          <Button
            size="sm"
            variant="outline"
            disabled={busy}
            onClick={() =>
              act(() => refreshDomainPolicy(rule.rule_id), "상태를 다시 읽었어요.")
            }
          >
            <Icon name="refresh" size={14} />
            상태 다시 읽기
          </Button>
          <Button size="sm" variant="destructive" disabled={busy} onClick={remove}>
            <Icon name="close" size={14} />
            삭제
          </Button>
        </div>

        {message ? (
          <p
            className={cn(
              "rounded border px-2 py-1 text-xs",
              failed
                ? "border-red-300 bg-red-50 text-red-800"
                : "border-emerald-300 bg-emerald-50 text-emerald-800",
            )}
          >
            {message}
          </p>
        ) : null}
        {dialog}
      </CardContent>
    </Card>
  );
}

export function DomainPolicyClient() {
  const [creating, setCreating] = useState(false);
  const rules = useSWR<DomainPolicyList>(
    "admin/identity/domain-policies",
    getDomainPolicies,
    { revalidateOnFocus: false },
  );
  const options = useSWR<DomainPolicyOptions>(
    "admin/identity/domain-policies/options",
    getDomainPolicyOptions,
    { revalidateOnFocus: false },
  );

  // 목록과 선택지 어느 쪽이 먼저 오든 좌표는 같아요 — 서버가 도출한 그 하나예요.
  const gatewayArn = rules.data?.gateway_arn || options.data?.gateway_arn || "";

  return (
    <div className="space-y-5">
      <PageHeading
        title="도메인 정책 (Cedar)"
        // ⚠️ 여기를 `inline-flex` 로 감싸면 `?` 가 **혼자 다음 줄로 떨어져요**(브라우저 실측).
        // 텍스트가 flex item 하나가 되니 마지막 낱말과 같이 흐르지 못하거든요. 그냥 인라인으로
        // 이어 붙여요 — `HelpPopover` 트리거가 `inline-flex … align-middle` 이라 문장에 붙어요.
        description={
          <>
            요청 <b>인자 값</b>에 걸는 규칙이에요 — 「환불 금액이 100,000원을 넘으면 안 된다」
            같은 것. <b>누가 무엇을 부를 수 있나</b>는 여기서 정하지 않아요(그건 도구 인가
            승인과 Gateway interceptor 가 판정해요). 항상{" "}
            <code className="font-mono">permit</code> + 가드 형태로만 만들어요.{" "}
            <HelpPopover label={PERMIT_SHAPE_HELP_LABEL}>
              <HelpParagraphs lines={PERMIT_SHAPE_HELP} />
            </HelpPopover>
          </>
        }
      />

      {/* 옛 파란 박스를 `?` 로 접었어요. **사실은 하나도 안 지웠어요** — `WHY_PERMIT_HELP` 에
          세 사실(평가 오류로 금지가 무력화됨 · `context.input` 이 열린 레코드 · 그래서
          permit + has 가드만, 인자 없으면 기본 거부)이 그대로 있어요. */}
      <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-2">
        <span className="inline-flex items-center gap-1 text-xs text-muted-foreground">
          {WHY_PERMIT_QUESTION}
          <HelpPopover label={WHY_PERMIT_HELP_LABEL}>
            <HelpParagraphs lines={WHY_PERMIT_HELP} />
          </HelpPopover>
        </span>
        <CedarExamplesButton gatewayArn={gatewayArn} />
      </div>

      {options.data?.warnings.length ? (
        <ul className="space-y-1 rounded-md border border-amber-300 bg-amber-50 p-3 text-sm text-amber-900">
          {options.data.warnings.map((warning) => (
            <li key={warning}>· {warning}</li>
          ))}
        </ul>
      ) : null}

      <div className="flex flex-wrap items-end justify-between gap-3">
        <GatewayChoice arn={gatewayArn} />
        <div className="flex items-center gap-2">
          <Button size="sm" variant="outline" onClick={() => void rules.mutate()}>
            <Icon name="refresh" size={14} />
            새로고침
          </Button>
          <Button
            size="sm"
            disabled={!options.data}
            onClick={() => setCreating((value) => !value)}
          >
            <Icon name={creating ? "back" : "plus"} size={14} />
            {creating ? "목록으로" : "도메인 규칙 추가"}
          </Button>
        </div>
      </div>

      {options.error ? (
        <LoadError
          message={`선택지를 읽지 못했어요: ${errorMessage(options.error, "알 수 없는 오류예요.")}`}
        />
      ) : null}

      {creating && options.data ? (
        <CreateForm
          options={options.data}
          onCreated={async () => {
            setCreating(false);
            await rules.mutate();
          }}
          onCancel={() => setCreating(false)}
        />
      ) : rules.error ? (
        <LoadError
          message={`정책 목록을 읽지 못했어요: ${errorMessage(rules.error, "알 수 없는 오류예요.")}`}
        />
      ) : rules.isLoading ? (
        <LoadingRows label="도메인 규칙을 읽고 있어요" />
      ) : rules.data && rules.data.rules.length === 0 ? (
        <EmptyState
          title="도메인 규칙이 없어요"
          description="인자 값에 걸 규칙을 추가하면 여기에 나와요. 만든 규칙은 LOG_ONLY 로 시작해요."
        />
      ) : (
        <div className="space-y-3">
          {(rules.data?.rules ?? []).map((rule) => (
            <RuleCard
              key={rule.rule_id}
              rule={rule}
              onChanged={async () => {
                await rules.mutate();
              }}
            />
          ))}
        </div>
      )}
    </div>
  );
}
