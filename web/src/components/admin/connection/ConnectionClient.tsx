"use client";

import { useState } from "react";
import useSWR from "swr";
import {
  ApiError,
  getConnection,
  setConnection,
  verifyConnection,
  type ConnectionState,
  type RepoConnection,
} from "@/lib/api";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Icon } from "@/components/ui/icon";
import { cn } from "@/lib/ui";

// 저장소 연결 상태 → 배지 색/라벨. 백엔드 status 문자열 그대로 오지만
// (CONNECTED 등) 미지의 값은 회색 outline 으로 안전하게 폴백해요.
const STATUS_STYLES: Record<string, { label: string; className: string }> = {
  CONNECTED: { label: "연결됨", className: "bg-emerald-100 text-emerald-700" },
  VERIFIED: { label: "검증됨", className: "bg-emerald-100 text-emerald-700" },
  ERROR: { label: "오류", className: "bg-red-100 text-red-700" },
};

function statusStyle(status: string) {
  return STATUS_STYLES[status] ?? { label: status, className: "bg-slate-100 text-slate-600" };
}

// provider 개념은 남겨두되(gitlab 확장 대비) 지금은 github 만 지원해요.
const PROVIDER_LABELS: Record<string, string> = { github: "GitHub", gitlab: "GitLab" };

export function ConnectionClient() {
  const { data, mutate } = useSWR<ConnectionState>("publish/connection", getConnection);
  const [reconnecting, setReconnecting] = useState(false);

  const connected = data != null && data.status !== "NOT_CONNECTED";
  const conn = connected ? (data as RepoConnection) : null;
  // 폼 노출 조건: 미연결이거나, 사용자가 "연결 변경"을 눌러 재연결 중일 때만.
  const showForm = !connected || reconnecting;

  return (
    <div className="max-w-3xl">
      <div className="mb-6">
        <h1 className="text-2xl font-bold tracking-tight">저장소 연결</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          플러그인을 배포할 Git 저장소를 연결해요. 배포는 이 저장소에 커밋으로 기록돼요.
        </p>
      </div>

      <div className="space-y-5">
        <StatusCard
          conn={conn}
          onReconnect={() => setReconnecting(true)}
          reconnecting={reconnecting}
        />

        {showForm && (
          <ConnectForm
            reconnecting={reconnecting}
            onConnected={async () => {
              setReconnecting(false);
              await mutate();
            }}
            onCancel={connected ? () => setReconnecting(false) : undefined}
          />
        )}
      </div>
    </div>
  );
}

// 현재 연결 상태 카드 — 연결됐으면 repo·provider·상태 배지, 아니면 빈 상태 안내.
function StatusCard({
  conn,
  onReconnect,
  reconnecting,
}: {
  conn: RepoConnection | null;
  onReconnect: () => void;
  reconnecting: boolean;
}) {
  const [verifyBusy, setVerifyBusy] = useState(false);
  const [verifyMsg, setVerifyMsg] = useState<{ ok: boolean; text: string } | null>(null);

  async function verify() {
    setVerifyBusy(true);
    setVerifyMsg(null);
    try {
      const res = await verifyConnection();
      setVerifyMsg({ ok: res.ok, text: res.ok ? "접근 정상" : res.reason });
    } catch (err) {
      setVerifyMsg({ ok: false, text: err instanceof Error ? err.message : "검증 실패" });
    } finally {
      setVerifyBusy(false);
    }
  }

  if (!conn) {
    return (
      <Card>
        <CardContent className="p-5">
          <div className="flex items-center gap-3">
            <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full bg-muted text-muted-foreground">
              <Icon name="link" size={18} />
            </span>
            <div>
              <div className="text-sm font-semibold text-foreground">연결된 저장소가 없어요</div>
              <p className="mt-0.5 text-sm text-muted-foreground">
                아래에서 GitHub 저장소를 연결하면 배포를 시작할 수 있어요.
              </p>
            </div>
          </div>
        </CardContent>
      </Card>
    );
  }

  const style = statusStyle(conn.status);
  const provider = PROVIDER_LABELS[conn.provider] ?? conn.provider;

  return (
    <Card>
      <div className="flex items-center justify-between border-b border-border p-5">
        <h3 className="text-base font-semibold leading-tight tracking-tight text-card-foreground">
          현재 연결
        </h3>
        <Badge variant="type" className={style.className}>
          {style.label}
        </Badge>
      </div>
      <CardContent className="p-5">
        <dl className="grid grid-cols-1 gap-x-6 gap-y-3 sm:grid-cols-[auto_1fr]">
          <dt className="text-xs text-muted-foreground sm:pt-0.5">제공자</dt>
          <dd className="text-sm font-medium text-foreground">{provider}</dd>

          <dt className="text-xs text-muted-foreground sm:pt-0.5">저장소</dt>
          <dd className="min-w-0">
            <a
              href={conn.repo_url}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex max-w-full items-center gap-1.5 break-all text-sm font-medium text-blue-600 transition-colors hover:text-blue-700 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-1"
            >
              <Icon name="link" size={14} className="shrink-0" />
              {conn.repo_url}
            </a>
          </dd>

          {conn.connected_at && (
            <>
              <dt className="text-xs text-muted-foreground sm:pt-0.5">연결 시각</dt>
              <dd className="text-sm text-foreground">{formatTs(conn.connected_at)}</dd>
            </>
          )}
        </dl>

        <div className="mt-5 flex flex-wrap items-center gap-2 border-t border-border pt-4">
          <Button variant="outline" size="sm" onClick={verify} disabled={verifyBusy}>
            {verifyBusy ? "확인 중…" : "연결 확인"}
          </Button>
          {!reconnecting && (
            <Button variant="ghost" size="sm" onClick={onReconnect}>
              연결 변경
            </Button>
          )}
          {verifyMsg && (
            <span
              className={cn(
                "inline-flex items-center gap-1 text-xs",
                verifyMsg.ok ? "text-emerald-700" : "text-red-600",
              )}
            >
              <Icon name={verifyMsg.ok ? "check" : "back"} size={13} className="shrink-0" />
              {verifyMsg.text}
            </span>
          )}
        </div>
      </CardContent>
    </Card>
  );
}

// 저장소 연결(GitHub) 입력 폼 — 미연결이거나 "연결 변경" 중일 때만 렌더돼요.
function ConnectForm({
  reconnecting,
  onConnected,
  onCancel,
}: {
  reconnecting: boolean;
  onConnected: () => void;
  onCancel?: () => void;
}) {
  const [repoUrl, setRepoUrl] = useState("");
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function connect() {
    setBusy(true);
    setError(null);
    try {
      await setConnection({ repo_url: repoUrl, token });
      setToken("");
      setRepoUrl("");
      onConnected();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : err instanceof Error ? err.message : "연결 실패");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card>
      <CardHeader className="border-b border-border">
        <CardTitle className="text-base">
          {reconnecting ? "저장소 다시 연결 (GitHub)" : "저장소 연결 (GitHub)"}
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-4 p-5">
        <div>
          <label htmlFor="repo-url" className="mb-1 block text-xs font-medium text-foreground">
            저장소 URL
          </label>
          <Input
            id="repo-url"
            value={repoUrl}
            onChange={(e) => setRepoUrl(e.target.value)}
            placeholder="https://github.com/owner/repo"
          />
        </div>
        <div>
          <label htmlFor="repo-token" className="mb-1 block text-xs font-medium text-foreground">
            액세스 토큰
          </label>
          <Input
            id="repo-token"
            value={token}
            onChange={(e) => setToken(e.target.value)}
            type="password"
            placeholder="fine-grained PAT (Contents: write)"
          />
          <p className="mt-1 text-xs text-muted-foreground">
            Contents 쓰기 권한이 있는 fine-grained PAT 가 필요해요. 토큰은 저장 후 다시 표시되지 않아요.
          </p>
        </div>

        {error && (
          <div className="rounded-lg border border-red-200 bg-red-50 p-3 text-sm text-red-700">
            {error}
          </div>
        )}

        <div className="flex gap-2 border-t border-border pt-4">
          <Button onClick={connect} disabled={busy || !repoUrl || !token}>
            {busy ? "연결 중…" : reconnecting ? "다시 연결" : "연결"}
          </Button>
          {onCancel && (
            <Button variant="outline" onClick={onCancel} disabled={busy}>
              취소
            </Button>
          )}
        </div>
      </CardContent>
    </Card>
  );
}

// ISO(UTC) → KST 표시 (memory: 모든 시간 KST). 파싱 실패 시 원문 폴백.
function formatTs(iso: string): string {
  try {
    return new Date(iso).toLocaleString("ko-KR", {
      timeZone: "Asia/Seoul",
      dateStyle: "medium",
      timeStyle: "short",
    });
  } catch {
    return iso;
  }
}
