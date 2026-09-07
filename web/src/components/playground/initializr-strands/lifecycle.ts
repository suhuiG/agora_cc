export type PromptRequest = {
  id: number;
  revision: number;
  signal: AbortSignal;
};

export class PromptRequestGate {
  private revision = 0;
  private nextId = 0;
  private active: (PromptRequest & { controller: AbortController }) | null = null;
  private disposed = false;

  begin(): PromptRequest {
    this.active?.controller.abort();
    const controller = new AbortController();
    const request = {
      id: ++this.nextId,
      revision: this.revision,
      signal: controller.signal,
      controller,
    };
    this.active = request;
    return request;
  }

  invalidate(): void {
    this.revision += 1;
    this.active?.controller.abort();
  }

  canApply(request: PromptRequest): boolean {
    return !this.disposed
      && this.active?.id === request.id
      && request.revision === this.revision
      && !request.signal.aborted;
  }

  finish(request: PromptRequest): boolean {
    if (this.active?.id !== request.id) return false;
    this.active = null;
    return !this.disposed;
  }

  dispose(): void {
    this.disposed = true;
    this.invalidate();
    this.active = null;
  }

  /** dispose() 이후 같은 인스턴스를 다시 쓸 수 있게 되살려요.
   *
   * React Strict Mode(dev)는 effect를 mount→cleanup→mount로 돌려서 cleanup의
   * dispose()가 gate를 닫아버려요. 컴포넌트는 ref가 null일 때만 gate를 만들기
   * 때문에 remount 후에도 같은 인스턴스가 남고, disposed가 true로 굳어요. 그러면
   * canApply()가 항상 false라 **생성된 system prompt가 조용히 버려지고**,
   * finish()도 false라 스피너가 영구히 걸려요(실측 2026-08-21: 서버는 14초 만에
   * 200을 반환했는데 화면은 계속 생성 중). mountedRef를 되살리는 것과 같은 짝이에요.
   */
  reset(): void {
    this.active?.controller.abort();
    this.active = null;
    this.disposed = false;
  }
}

export type NameAvailability =
  | "unchecked"
  | "checking"
  | "available"
  | "unavailable"
  | "unknown";

export function nameAvailabilityAfterCheck(
  available: boolean | undefined,
): NameAvailability {
  if (available === undefined) return "unknown";
  return available ? "available" : "unavailable";
}

export function isNameDeployable(state: NameAvailability): boolean {
  return state === "available";
}

export const DEPLOY_PHASES = [
  { phase: "QUEUED", label: "요청 접수" },
  { phase: "BUILDING", label: "코드 빌드" },
  { phase: "DEPLOYING", label: "Runtime 배포" },
  // 「인가 배선」이었어요 — 「배선」이 관리자·고객에게 안 읽히는 말이라 바꿨어요. 이 라벨은
  // `DeployProgressStepper` 도 같이 써요(문구가 갈라지면 안 되니까 상수 하나예요).
  { phase: "PROVISIONING", label: "인가 로직 생성" },
  { phase: "VERIFYING", label: "도구 검증" },
  { phase: "READY", label: "완료" },
] as const;

export function authorizationVerificationLabel(report: {
  verdict?: string;
  negative_control_verdict?: "denied" | "allowed" | "unknown";
}): string | null {
  if (
    report.verdict === "unknown_authorization"
    || report.negative_control_verdict === "unknown"
  ) {
    return "인가 거부 증거 미관측(unknown)";
  }
  // v4 cannot produce "denied"; IH-48 must add trusted evidence before UI copy.
  if (report.negative_control_verdict === "allowed") {
    return "미승인 도구 호출 허용됨";
  }
  return null;
}

export type BuiltinObservationReport = {
  ok?: boolean;
  verdict?: string;
  resources?: Record<string, string>;
  spans?: string;
  metrics?: string;
  reason?: string;
};

export type BuiltinObservationStatus = {
  tone: "unknown" | "failed";
  label: string;
  detail: string;
};

export function builtinObservationStatus(
  report: BuiltinObservationReport | null | undefined,
): BuiltinObservationStatus | null {
  if (!report || report.verdict === "not_applicable") return null;
  if (report.ok === false || report.verdict === "diverged") {
    return {
      tone: "failed",
      label: "내장 도구 관측 설정 불일치",
      detail: report.reason || "제어플레인 설정이 배포 선언과 달라요.",
    };
  }
  if (report.verdict === "unknown" || report.spans === "unknown") {
    const resourceCount = Object.keys(report.resources ?? {}).length;
    return {
      tone: "unknown",
      label: "내장 도구 span 미관측",
      detail: (
        `리소스 설정 ${resourceCount}개 확인`
        + " · 메트릭은 첫 사용 후 Resource ID로 분리"
      ),
    };
  }
  return null;
}
