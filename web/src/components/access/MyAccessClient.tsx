"use client";

import useSWR from "swr";
import { AssetPoliciesPanel } from "@/components/access/AssetPoliciesPanel";
import { Badge } from "@/components/ui/badge";
import {
  listAvailableAccessConnections,
  listMyAccessGrants,
  type AccessGrant,
} from "@/lib/api";
import { cn } from "@/lib/ui";

export function MyAccessClient() {
  const {
    data: grants,
    error: grantsError,
    isLoading: grantsLoading,
  } = useSWR("me/access-grants", listMyAccessGrants);
  const { data: connections } = useSWR(
    "access/connections",
    listAvailableAccessConnections,
  );
  const connectionNames = new Map(
    connections?.map((item) => [item.connection_id, item.name]) ?? [],
  );

  return (
    <div className="mx-auto max-w-6xl space-y-9">
      <div>
        <h1 className="text-2xl font-bold">내 접근 권한</h1>
        <p className="mt-1 max-w-3xl text-sm text-muted-foreground">
          Agent와 MCP가 호출 시 승계하는 내 업무 시스템 권한을 확인해요.
        </p>
      </div>

      <section aria-labelledby="my-grants-title">
        <div className="mb-4">
          <h2 id="my-grants-title" className="text-base font-semibold">
            부여된 권한
          </h2>
          <p className="mt-1 text-sm text-muted-foreground">
            권한 변경과 회수는 다음 호출부터 즉시 적용됩니다.
          </p>
        </div>

        {grantsLoading ? (
          <LoadingRows />
        ) : grantsError ? (
          <LoadError message="내 권한을 불러오지 못했어요." />
        ) : grants?.length === 0 ? (
          <EmptyState
            title="부여된 접근 권한이 없어요."
            description="업무 시스템 권한이 필요하면 Agora 관리자에게 요청하세요."
          />
        ) : (
          <div className="divide-y divide-border rounded-lg border border-border">
            {grants?.map((grant) => (
              <GrantRow
                key={grant.grant_id}
                grant={grant}
                connectionName={
                  connectionNames.get(grant.connection_id) ?? grant.connection_id
                }
              />
            ))}
          </div>
        )}
      </section>

      <section className="border-t border-border pt-8" aria-labelledby="asset-policy-title">
        {/* `mode` prop 을 없앴어요 (IH-162 · ADR-0112) — 저장소 전체에서 항상 `"owner"` 였고,
            PENDING/APPROVED 를 가르는 실제 판별자는 서버의 `principal.is_admin` 이에요. */}
        <AssetPoliciesPanel />
      </section>
    </div>
  );
}

function GrantRow({
  grant,
  connectionName,
}: {
  grant: AccessGrant;
  connectionName: string;
}) {
  return (
    <div className="grid min-w-0 gap-3 px-4 py-4 md:grid-cols-[minmax(0,1fr)_minmax(0,1.4fr)_auto] md:items-center">
      <div className="min-w-0">
        <div className="flex flex-wrap items-center gap-2">
          <span className="break-all text-sm font-semibold">{connectionName}</span>
          <StatusBadge status={effectiveGrantStatus(grant)} />
        </div>
        <p className="mt-1 break-all text-xs text-muted-foreground">
          {grant.connection_id}
        </p>
      </div>
      <div className="flex min-w-0 flex-wrap gap-1.5">
        {grant.capabilities.map((capability) => (
          <Badge
            key={capability}
            variant="outline"
            className="max-w-full break-all"
          >
            {capability}
          </Badge>
        ))}
      </div>
      <span className="text-xs text-muted-foreground md:text-right">
        {grant.expires_at ? formatEpoch(grant.expires_at) : "만료 없음"}
      </span>
    </div>
  );
}

function StatusBadge({ status }: { status: string }) {
  const normalized = status.toUpperCase();
  const style =
    normalized === "ACTIVE"
      ? "bg-emerald-100 text-emerald-700"
      : normalized === "REVOKED" || normalized === "EXPIRED"
        ? "bg-red-100 text-red-700"
        : "bg-slate-100 text-slate-600";
  return (
    <Badge variant="type" className={cn("shrink-0", style)}>
      {status}
    </Badge>
  );
}

function LoadingRows() {
  return (
    <div role="status" aria-label="내 권한 불러오는 중" className="space-y-2">
      {Array.from({ length: 2 }).map((_, index) => (
        <div key={index} className="h-16 animate-pulse rounded-lg bg-muted/60" />
      ))}
    </div>
  );
}

function LoadError({ message }: { message: string }) {
  return (
    <div
      role="alert"
      className="rounded-lg border border-red-200 bg-red-50 p-4 text-sm text-red-700"
    >
      {message}
    </div>
  );
}

function EmptyState({
  title,
  description,
}: {
  title: string;
  description: string;
}) {
  return (
    <div className="rounded-lg border border-dashed border-border px-4 py-9 text-center">
      <div className="text-sm font-medium">{title}</div>
      <p className="mx-auto mt-1 max-w-xl text-sm text-muted-foreground">
        {description}
      </p>
    </div>
  );
}

function formatEpoch(value: number): string {
  return new Intl.DateTimeFormat("ko-KR", {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value * 1000));
}

function effectiveGrantStatus(grant: AccessGrant): string {
  if (
    grant.status === "ACTIVE" &&
    grant.expires_at &&
    grant.expires_at <= Math.floor(Date.now() / 1000)
  ) {
    return "EXPIRED";
  }
  return grant.status;
}
