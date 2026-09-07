"use client";

import { useCallback, useEffect, useState } from "react";
import { currentReturnTo, getAuthSession, type AuthSession } from "@/lib/auth-client";

type SessionState =
  | { status: "loading" }
  | { status: "ready"; session: AuthSession }
  | { status: "error" };

function initials(principal: string): string {
  const parts = principal
    .split(/[\s.@_-]+/)
    .map((part) => part.trim())
    .filter(Boolean);

  if (parts.length === 0) {
    return "U";
  }
  return parts
    .slice(0, 2)
    .map((part) => part[0])
    .join("")
    .toUpperCase();
}

export function AuthControls() {
  const [state, setState] = useState<SessionState>({ status: "loading" });
  const [isLoggingOut, setIsLoggingOut] = useState(false);

  const loadSession = useCallback((signal?: AbortSignal) => {
    setState({ status: "loading" });
    getAuthSession(signal)
      .then((session) => setState({ status: "ready", session }))
      .catch((error: unknown) => {
        if (error instanceof DOMException && error.name === "AbortError") {
          return;
        }
        setState({ status: "error" });
      });
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    getAuthSession(controller.signal)
      .then((session) => setState({ status: "ready", session }))
      .catch((error: unknown) => {
        if (error instanceof DOMException && error.name === "AbortError") {
          return;
        }
        setState({ status: "error" });
      });
    return () => controller.abort();
  }, []);

  if (state.status === "loading") {
    return (
      <div
        className="h-8 w-20 animate-pulse rounded-lg bg-muted sm:w-40"
        aria-label="사용자 세션 확인 중"
      />
    );
  }

  if (state.status === "error") {
    return (
      <div className="flex h-8 items-center gap-2 text-xs">
        <span className="hidden text-red-700 sm:inline">세션 오류</span>
        <button
          type="button"
          onClick={() => loadSession()}
          className="h-8 rounded-lg border border-input bg-card px-3 font-medium hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          재시도
        </button>
      </div>
    );
  }

  if (!state.session.authenticated) {
    return (
      <button
        type="button"
        onClick={() =>
          window.location.assign(`/login?returnTo=${encodeURIComponent(currentReturnTo())}`)
        }
        className="inline-flex h-8 items-center justify-center rounded-lg border border-input bg-card px-3 text-xs font-medium hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        로그인
      </button>
    );
  }

  const name =
    state.session.display_name?.trim() ||
    state.session.principal_id?.trim() ||
    "로그인 사용자";
  const email = state.session.email?.trim();
  const role = state.session.roles?.includes("admin") ? "admin" : "user";
  // 큰 줄엔 이름, 작은 줄엔 email(있으면). email이 없거나 이름과 같으면 역할을 보여줘요.
  const subline = email && email !== name ? email : role;

  function logout() {
    setIsLoggingOut(true);
    window.location.assign("/api/auth/logout");
  }

  return (
    <div className="flex min-w-0 items-center gap-2">
      <span
        className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-primary text-xs font-semibold text-primary-foreground"
        aria-hidden="true"
      >
        {initials(name)}
      </span>
      <span className="hidden min-w-0 sm:block">
        <span className="block max-w-40 truncate text-sm font-semibold text-foreground" title={name}>
          {name}
        </span>
        <span
          className="block max-w-40 truncate text-[11px] leading-tight text-muted-foreground"
          title={subline}
        >
          {subline}
        </span>
      </span>
      <button
        type="button"
        onClick={logout}
        disabled={isLoggingOut}
        className="h-8 shrink-0 rounded-lg px-2 text-xs font-medium text-muted-foreground hover:bg-accent hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50 sm:px-3"
      >
        {isLoggingOut ? "처리 중" : "로그아웃"}
      </button>
    </div>
  );
}
