"use client";

// grants·capability-sets·자산 정책 화면이 공유하는 표현 컴포넌트와 순수 함수.
// AccessControlClient 한 파일(1531행)에 몰려 있던 것을 화면 분리(S2) 때 꺼냈어요.
// 여기에는 상태·데이터 접근이 없어요 — 화면별 컨테이너가 각자 들고 있어요.

import {
  ApiError,
  type AccessCapability,
  type AccessCapabilityList,
  type AccessGrant,
} from "@/lib/api";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { accessCapabilityItems } from "@/lib/adminAccessCapabilities";
import { cn } from "@/lib/ui";

export function SectionHeading({
  title,
  description,
}: {
  title: string;
  description: string;
}) {
  return (
    <div>
      <h2 className="text-base font-semibold">{title}</h2>
      <p className="mt-1 text-sm text-muted-foreground">{description}</p>
    </div>
  );
}

export function PageHeading({
  title,
  description,
}: {
  title: string;
  description: React.ReactNode;
}) {
  return (
    <div className="mb-5">
      <h1 className="text-2xl font-bold tracking-tight">{title}</h1>
      {/* `break-keep` = `word-break: keep-all` — 한국어 낱말이 줄바꿈에서 갈라지지 않게요.
          기본값이면 브라우저가 CJK 를 «글자 단위» 로 끊어서 「부여」가 「부 / 여」로 갈려요
          (2026-09-06 브라우저 실측, 1600·1280 둘 다). 고객 앞 화면이라 고쳐 둬요. */}
      <p className="mt-1 max-w-3xl break-keep text-sm text-muted-foreground">
        {description}
      </p>
    </div>
  );
}

export function Field({
  label,
  htmlFor,
  hint,
  required,
  children,
}: {
  label: string;
  htmlFor: string;
  hint?: string;
  required?: boolean;
  children: React.ReactNode;
}) {
  return (
    <label htmlFor={htmlFor} className="block min-w-0">
      <span className="mb-1.5 flex flex-wrap items-center gap-x-1 text-xs font-medium">
        {label}
        {required && <span className="text-red-600">*</span>}
        {hint && <span className="font-normal text-muted-foreground">· {hint}</span>}
      </span>
      {children}
    </label>
  );
}

export function FormActions({
  busy,
  error,
  onCancel,
  submitLabel = "저장",
}: {
  busy: boolean;
  error: string;
  onCancel: () => void;
  submitLabel?: string;
}) {
  return (
    <div className="flex flex-wrap items-center gap-3 border-t border-border pt-4">
      <Button type="submit" size="sm" disabled={busy}>
        {busy ? "저장 중…" : submitLabel}
      </Button>
      <Button
        type="button"
        variant="outline"
        size="sm"
        onClick={onCancel}
        disabled={busy}
      >
        취소
      </Button>
      {error && <InlineError message={error} />}
    </div>
  );
}

export function StatusBadge({ status }: { status: string }) {
  const normalized = status.toUpperCase();
  const style =
    normalized === "ACTIVE" || normalized === "APPROVED"
      ? "bg-emerald-100 text-emerald-700"
      : normalized === "PENDING"
        ? "bg-amber-100 text-amber-800"
        : normalized === "REVOKED" ||
            normalized === "REJECTED" ||
            normalized === "EXPIRED"
          ? "bg-red-100 text-red-700"
          : "bg-slate-100 text-slate-600";
  return (
    <Badge variant="type" className={cn("shrink-0", style)}>
      {status}
    </Badge>
  );
}

export function TagList({ items }: { items: string[] }) {
  return (
    <div className="flex min-w-0 flex-wrap gap-1.5">
      {items.map((item) => (
        <Badge key={item} variant="outline" className="max-w-full break-all">
          {item}
        </Badge>
      ))}
    </div>
  );
}

export function LoadingRows({ label, count = 3 }: { label: string; count?: number }) {
  return (
    <div role="status" aria-label={label} className="space-y-2">
      {Array.from({ length: count }).map((_, index) => (
        <div key={index} className="h-14 animate-pulse rounded-lg bg-muted/60" />
      ))}
    </div>
  );
}

export function LoadError({ message }: { message: string }) {
  return (
    <div
      role="alert"
      className="rounded-lg border border-red-200 bg-red-50 p-4 text-sm text-red-700"
    >
      {message}
    </div>
  );
}

export function InlineError({ message }: { message: string }) {
  return (
    <span role="alert" className="min-w-0 break-words text-sm text-red-600">
      {message}
    </span>
  );
}

export function EmptyState({
  title,
  description,
  action,
}: {
  title: string;
  description: string;
  action?: React.ReactNode;
}) {
  return (
    <div className="rounded-lg border border-dashed border-border px-4 py-10 text-center">
      <div className="text-sm font-medium">{title}</div>
      <p className="mx-auto mt-1 max-w-xl text-sm text-muted-foreground">{description}</p>
      {action && <div className="mt-4">{action}</div>}
    </div>
  );
}

export const textAreaClass =
  "w-full min-w-0 rounded-lg border border-input bg-card px-3.5 py-2 text-sm text-foreground placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-1";

export function parseList(value: string): string[] {
  return Array.from(
    new Set(value.split(/[,\n]/).map((item) => item.trim()).filter(Boolean)),
  );
}

export function errorMessage(error: unknown, fallback: string): string {
  if (error instanceof ApiError || error instanceof Error) return error.message;
  return fallback;
}

/** 낙관적 락 충돌(409) 여부 — "다른 사람이 먼저 저장함" 안내·재조회 분기에 써요(결함 #9). */
export function isConflict(error: unknown): boolean {
  return error instanceof ApiError && error.status === 409;
}

export function formatEpoch(value: number): string {
  return new Intl.DateTimeFormat("ko-KR", {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value * 1000));
}

/**
 * datetime-local 값을 epoch 초로 바꾸고 과거인지 판정해요.
 *
 * `Date.now()` 를 컴포넌트 본문에서 부르면 react-hooks/purity 가 막아요 (렌더 중
 * 불순 함수 호출). 이벤트 핸들러에서만 쓰이도록 모듈 수준 함수로 빼 뒀어요.
 * 반환값이 null 이면 입력이 비었거나 파싱 불가·과거예요.
 */
export function futureEpochFrom(value: string): { epoch?: number; invalid: boolean } {
  if (!value) return { invalid: false };
  const epoch = Math.floor(new Date(value).getTime() / 1000);
  if (!Number.isFinite(epoch) || epoch <= Math.floor(Date.now() / 1000)) {
    return { invalid: true };
  }
  return { epoch, invalid: false };
}

/** 만료 시각이 지난 ACTIVE grant 는 화면에서 EXPIRED 로 보여줘요(서버 상태는 그대로). */
export function effectiveGrantStatus(grant: AccessGrant): string {
  if (
    grant.status === "ACTIVE" &&
    grant.expires_at &&
    grant.expires_at <= Math.floor(Date.now() / 1000)
  ) {
    return "EXPIRED";
  }
  return grant.status;
}

/**
 * 권한 그룹이 실제로 부여하는 capability 전체 — ACTIVE ∩ ceiling.
 *
 * 부분 부여를 없앴으니 이 목록이 곧 "그룹을 고르면 부여되는 것"이에요
 * (docs/design/admin-console-spec.md §5.2). 판정 7·8번이 각각
 * ACTIVE 와 ceiling 을 보므로 두 조건을 모두 만족하는 것만 담아야
 * 저장 직후 DENY 되는 조합을 만들지 않아요.
 */
export function capsOf(
  capabilities: AccessCapability[] | AccessCapabilityList | undefined,
  ceiling: string[],
): string[] {
  const allowed = new Set(ceiling);
  return accessCapabilityItems(capabilities)
    .filter((item) => item.status === "ACTIVE" && allowed.has(item.name))
    .map((item) => item.name);
}
