"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import {
  currentReturnTo,
  getAuthSession,
  type AuthSession,
} from "@/lib/auth-client";

type GuardState =
  | { status: "loading" }
  | { status: "ready"; session: AuthSession }
  | { status: "error" };

export function AdminGuard({
  children,
  embedded = false,
}: {
  children: React.ReactNode;
  embedded?: boolean;
}) {
  const [state, setState] = useState<GuardState>({ status: "loading" });
  const frameClass = embedded
    ? "flex min-h-[40vh] items-center justify-center px-5"
    : "flex min-h-screen items-center justify-center px-5";

  useEffect(() => {
    const controller = new AbortController();
    getAuthSession(controller.signal)
      .then((session) => {
        if (!session.authenticated) {
          window.location.replace(
            `/login?returnTo=${encodeURIComponent(currentReturnTo())}`,
          );
          return;
        }
        setState({ status: "ready", session });
      })
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
        role="status"
        className={`${frameClass} text-sm text-muted-foreground`}
      >
        권한 확인 중
      </div>
    );
  }

  if (state.status === "error") {
    return (
      <div className={frameClass}>
        <p className="text-sm text-red-700">권한을 확인하지 못했습니다.</p>
      </div>
    );
  }

  if (!state.session.roles?.includes("admin")) {
    return (
      <div className={frameClass}>
        <section className="w-full max-w-md" aria-labelledby="access-denied-title">
          <p className="text-xs font-medium text-red-700">403</p>
          <h1 id="access-denied-title" className="mt-2 text-xl font-semibold">
            관리자 권한이 필요합니다
          </h1>
          <p className="mt-2 text-sm text-muted-foreground">
            현재 계정은 user 역할로 로그인되어 있습니다.
          </p>
          <Link
            href="/catalog/browse"
            className="mt-6 inline-flex h-9 items-center justify-center rounded-lg border border-input bg-card px-3 text-sm font-medium hover:bg-accent"
          >
            카탈로그로 이동
          </Link>
        </section>
      </div>
    );
  }

  return children;
}
