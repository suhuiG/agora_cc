"use client";

import { useEffect, useRef, useState } from "react";
import type { FormEvent, ReactNode } from "react";
import { useRouter } from "next/navigation";
import { Button } from "@/components/ui/button";
import { Icon } from "@/components/ui/icon";
import { Input } from "@/components/ui/input";
import {
  clearSrpCache,
  confirmPasswordReset,
  requestPasswordReset,
  signIn,
} from "@/lib/auth-srp";
import type { SrpTokens } from "@/lib/auth-srp";
import {
  getAuthSession,
  loginUrl,
  postSession,
  safeReturnTo,
} from "@/lib/auth-client";
import { messageForLoginError } from "@/lib/auth/login-errors";

type LoginMode =
  | "checking"
  | "form"
  | "new_password"
  | "forgot"
  | "reset";

const AUTH_ERROR_MESSAGES: Record<string, string> = {
  invalid_callback: "로그인 응답이 올바르지 않아요. 다시 시도해 주세요.",
  expired_state: "로그인 요청이 만료됐어요. 다시 시도해 주세요.",
  token_exchange_failed: "로그인 토큰 교환에 실패했어요. 다시 시도해 주세요.",
  identity_rejected: "계정 정보를 확인하지 못했어요. 관리자에게 문의해 주세요.",
};

export default function LoginClient() {
  const router = useRouter();
  const returnTo = useRef("/catalog/browse");
  const pendingComplete = useRef<
    ((newPassword: string) => Promise<SrpTokens>) | null
  >(null);
  const [mode, setMode] = useState<LoginMode>("checking");
  const [busy, setBusy] = useState(false);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [code, setCode] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    const params = new URLSearchParams(window.location.search);
    returnTo.current = safeReturnTo(params.get("returnTo"));
    const authError = params.get("authError");
    if (authError) {
      Promise.resolve().then(() => {
        if (controller.signal.aborted) {
          return;
        }
        setError(
          AUTH_ERROR_MESSAGES[authError] ??
            "로그인에 실패했어요. 다시 시도해 주세요.",
        );
        setMode("form");
      });
      return () => controller.abort();
    }

    getAuthSession(controller.signal)
      .then((session) => {
        if (session.authenticated) {
          router.replace(returnTo.current);
          return;
        }
        setMode("form");
      })
      .catch((sessionError: unknown) => {
        if (
          sessionError instanceof DOMException &&
          sessionError.name === "AbortError"
        ) {
          return;
        }
        setError("세션을 확인하지 못했습니다. 로그인은 계속할 수 있어요.");
        setMode("form");
      });

    return () => controller.abort();
  }, [router]);

  async function promoteTokens(tokens: SrpTokens) {
    try {
      await postSession(tokens);
    } finally {
      // 세션 승격이 거부되거나 실패해도 refresh token 캐시를 브라우저에 남기지 않는다.
      await clearSrpCache();
    }
    router.replace(returnTo.current);
  }

  async function handleSignIn(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);
    setNotice(null);
    setBusy(true);
    try {
      const result = await signIn(email.trim(), password);
      if (result.status === "new_password_required") {
        pendingComplete.current = result.complete;
        setPassword("");
        setMode("new_password");
        return;
      }
      await promoteTokens(result.tokens);
    } catch (signInError) {
      setError(messageForLoginError(signInError));
    } finally {
      setBusy(false);
    }
  }

  async function handleNewPassword(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);
    const complete = pendingComplete.current;
    if (!complete) {
      setError("로그인 요청이 만료됐어요. 처음부터 다시 시도해 주세요.");
      setMode("form");
      return;
    }

    setBusy(true);
    try {
      const tokens = await complete(newPassword);
      await promoteTokens(tokens);
    } catch (challengeError) {
      setError(messageForLoginError(challengeError));
    } finally {
      setBusy(false);
    }
  }

  async function handleForgotPassword(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);
    setNotice(null);
    setBusy(true);
    try {
      await requestPasswordReset(email.trim());
      setMode("reset");
      setNotice("이메일로 받은 인증 코드를 입력해 주세요.");
    } catch (resetError) {
      setError(messageForLoginError(resetError));
    } finally {
      setBusy(false);
    }
  }

  async function handleConfirmReset(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);
    setBusy(true);
    try {
      await confirmPasswordReset(email.trim(), code.trim(), newPassword);
      setPassword("");
      setNewPassword("");
      setCode("");
      setMode("form");
      setNotice("비밀번호를 변경했어요. 새 비밀번호로 로그인해 주세요.");
    } catch (resetError) {
      setError(messageForLoginError(resetError));
    } finally {
      setBusy(false);
    }
  }

  function backToSignIn() {
    pendingComplete.current = null;
    setError(null);
    setNotice(null);
    setPassword("");
    setNewPassword("");
    setCode("");
    setMode("form");
  }

  const title =
    mode === "new_password"
      ? "새 비밀번호 설정"
      : mode === "forgot"
        ? "비밀번호 재설정"
        : mode === "reset"
          ? "인증 코드 입력"
          : "Agora 로그인";

  return (
    <main className="flex min-h-screen flex-col bg-background">
      <header className="flex h-14 items-center border-b border-border bg-card px-5">
        <span className="text-lg font-extrabold tracking-tight">Agora</span>
      </header>

      <div className="flex flex-1 items-center justify-center px-5 py-10">
        <section className="w-full max-w-sm" aria-labelledby="login-title">
          <div className="mb-5 flex h-10 w-10 items-center justify-center rounded-lg border border-border bg-card">
            <Icon name="shield" size={19} className="text-foreground" />
          </div>
          <h1
            id="login-title"
            className="text-2xl font-semibold text-foreground"
          >
            {title}
          </h1>
          <p className="mt-2 text-sm leading-6 text-muted-foreground">
            {mode === "new_password"
              ? "임시 비밀번호를 사용할 수 없도록 새 비밀번호를 설정하세요."
              : mode === "forgot"
                ? "가입한 이메일로 비밀번호 재설정 코드를 보내드려요."
                : mode === "reset"
                  ? "인증 코드와 새 비밀번호를 입력하세요."
                  : "Agora 계정으로 로그인하세요."}
          </p>

          <div className="mt-7 border-t border-border pt-6">
            {mode === "checking" ? (
              <div
                className="flex h-10 items-center gap-3 text-sm text-muted-foreground"
                role="status"
              >
                <span className="h-4 w-4 shrink-0 animate-spin rounded-full border-2 border-input border-t-foreground" />
                세션 확인 중
              </div>
            ) : (
              <>
                {notice && (
                  <p
                    className="mb-4 rounded-lg border border-blue-200 bg-blue-50 px-3 py-2.5 text-sm text-blue-800"
                    role="status"
                  >
                    {notice}
                  </p>
                )}
                {error && (
                  <p
                    className="mb-4 rounded-lg border border-red-200 bg-red-50 px-3 py-2.5 text-sm text-red-800"
                    role="alert"
                  >
                    {error}
                  </p>
                )}

                {mode === "form" && (
                  <form onSubmit={handleSignIn} className="space-y-4">
                    <Field label="이메일" htmlFor="login-email">
                      <Input
                        id="login-email"
                        name="email"
                        type="email"
                        autoComplete="username"
                        value={email}
                        onChange={(event) => setEmail(event.target.value)}
                        required
                        autoFocus
                      />
                    </Field>
                    <Field
                      label="비밀번호"
                      htmlFor="login-password"
                      action={
                        <button
                          type="button"
                          className="text-xs font-medium text-blue-700 hover:text-blue-900 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                          onClick={() => {
                            setError(null);
                            setNotice(null);
                            setMode("forgot");
                          }}
                        >
                          비밀번호를 잊으셨나요?
                        </button>
                      }
                    >
                      <Input
                        id="login-password"
                        name="password"
                        type="password"
                        autoComplete="current-password"
                        value={password}
                        onChange={(event) => setPassword(event.target.value)}
                        required
                      />
                    </Field>
                    <Button
                      type="submit"
                      className="w-full"
                      disabled={busy}
                    >
                      {busy ? "로그인 중" : "로그인"}
                    </Button>
                  </form>
                )}

                {mode === "new_password" && (
                  <form onSubmit={handleNewPassword} className="space-y-4">
                    <Field label="새 비밀번호" htmlFor="challenge-password">
                      <Input
                        id="challenge-password"
                        type="password"
                        autoComplete="new-password"
                        minLength={12}
                        value={newPassword}
                        onChange={(event) => setNewPassword(event.target.value)}
                        required
                        autoFocus
                      />
                    </Field>
                    <Button
                      type="submit"
                      className="w-full"
                      disabled={busy}
                    >
                      {busy ? "설정 중" : "비밀번호 설정"}
                    </Button>
                    <Button
                      type="button"
                      variant="ghost"
                      className="w-full"
                      onClick={backToSignIn}
                      disabled={busy}
                    >
                      로그인으로 돌아가기
                    </Button>
                  </form>
                )}

                {mode === "forgot" && (
                  <form onSubmit={handleForgotPassword} className="space-y-4">
                    <Field label="이메일" htmlFor="forgot-email">
                      <Input
                        id="forgot-email"
                        name="email"
                        type="email"
                        autoComplete="username"
                        value={email}
                        onChange={(event) => setEmail(event.target.value)}
                        required
                        autoFocus
                      />
                    </Field>
                    <Button
                      type="submit"
                      className="w-full"
                      disabled={busy}
                    >
                      {busy ? "전송 중" : "인증 코드 받기"}
                    </Button>
                    <Button
                      type="button"
                      variant="ghost"
                      className="w-full"
                      onClick={backToSignIn}
                      disabled={busy}
                    >
                      로그인으로 돌아가기
                    </Button>
                  </form>
                )}

                {mode === "reset" && (
                  <form onSubmit={handleConfirmReset} className="space-y-4">
                    <Field label="인증 코드" htmlFor="reset-code">
                      <Input
                        id="reset-code"
                        inputMode="numeric"
                        autoComplete="one-time-code"
                        value={code}
                        onChange={(event) => setCode(event.target.value)}
                        required
                        autoFocus
                      />
                    </Field>
                    <Field label="새 비밀번호" htmlFor="reset-password">
                      <Input
                        id="reset-password"
                        type="password"
                        autoComplete="new-password"
                        minLength={12}
                        value={newPassword}
                        onChange={(event) => setNewPassword(event.target.value)}
                        required
                      />
                    </Field>
                    <Button
                      type="submit"
                      className="w-full"
                      disabled={busy}
                    >
                      {busy ? "변경 중" : "비밀번호 변경"}
                    </Button>
                    <Button
                      type="button"
                      variant="ghost"
                      className="w-full"
                      onClick={backToSignIn}
                      disabled={busy}
                    >
                      로그인으로 돌아가기
                    </Button>
                  </form>
                )}

                {mode === "form" && (
                  <div className="mt-6 border-t border-border pt-5">
                    <Button
                      type="button"
                      variant="outline"
                      className="w-full"
                      onClick={() =>
                        window.location.assign(loginUrl(returnTo.current))
                      }
                      disabled={busy}
                    >
                      <Icon name="external" size={16} />
                      Cognito로 로그인
                    </Button>
                  </div>
                )}
              </>
            )}
          </div>
        </section>
      </div>
    </main>
  );
}

function Field({
  label,
  htmlFor,
  action,
  children,
}: {
  label: string;
  htmlFor: string;
  action?: ReactNode;
  children: ReactNode;
}) {
  return (
    <div>
      <div className="mb-1.5 flex items-center justify-between gap-3">
        <label
          htmlFor={htmlFor}
          className="text-sm font-medium text-foreground"
        >
          {label}
        </label>
        {action}
      </div>
      {children}
    </div>
  );
}
