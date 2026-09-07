import type { ApiErrorDetail } from "../api/client";

/**
 * Playground 호출 실패를 «사람 말 + 처방» 으로 옮겨요. 원문은 지우지 않아요.
 *
 * 왜 필요한가 (실측 2026-09-06, 시연 하루 전 화면):
 *
 * ```
 * 오류: 에이전트 오류: 모델 호출 실패: MaxTokensReachedException: Model stopped
 * generating due to maximum token limit. … For more information see:
 * https://strandsagents.com/docs/user-guide/concepts/agents/agent-loop...[truncated]
 * ```
 *
 * 이 문자열은 세 층을 그대로 통과해 온 거예요 — 생성 agent 의
 * `scaffold.py` `모델 호출 실패: {_exception_reason(e)}` → 포털의
 * `invoke_service.py` `에이전트 오류: {msg}` → 이 파일의 `오류: ${message}`. 사용자에게는
 * 라이브러리 내부 예외 이름과 **외부 문서 링크**만 남고, 무엇을 하면 되는지는 하나도 없어요.
 *
 * 그래서 «알려진 예외 → 한국어 처방» 을 `KNOWN_INVOKE_FAILURES` **한 곳에** 모아요. 저장소
 * 선례는 `gateway_interceptor.DENIAL_MESSAGES` 예요 — 사유별 한국어 문장을 한 상수에 모아
 * 두는 규약이죠.
 *
 * 세 가지를 지켜요.
 *
 * ① **원문을 버리지 않아요.** 예외 문구가 진단의 유일한 단서인 경우가 있어요. 사람 말 문장을
 *    앞에 두고 원문은 `raw` 로 항상 들고 다녀요(화면은 접어서 그려요).
 * ② **모르는 예외는 지금처럼 그대로 보여줘요.** 「알 수 없는 오류예요」로 뭉개면 관측한 것을
 *    버리는 거예요 — 그건 더 나빠요.
 * ③ **사람 말 문장에는 외부 링크를 넣지 않아요.** 링크는 원문 안에 그대로 남아 있어요.
 *
 * ⚠️ **추측으로 매핑을 늘리지 마세요.** 여기 있는 항목은 전부 (a) 그 문자열을 만드는 코드를
 * 확인했고 (b) 라이브 CloudWatch 에서 실제로 관측한 것만이에요. 각 항목의 주석에 근거를 적어
 * 뒀어요.
 */

export type InvokeFailureExplanation = {
  /** 사람 말 한 줄. 매핑에 없으면 원문 그대로예요. */
  headline: string;
  /** 가벼운 처방부터 순서대로. 무거운 것(재배포)은 «마지막» 이에요. */
  steps: readonly string[];
  /** 매핑에 걸린 신호 이름. 화면에는 안 나가요(진단·테스트용). 모르면 `null`. */
  signature: string | null;
  /** 관측한 원문. 언제나 채워요. */
  raw: string;
};

/** 접힌 원문 블록의 제목. 화면과 텍스트 형태가 같은 문구를 써요. */
export const INVOKE_FAILURE_RAW_LABEL = "자세히 — 오류 원문";

type KnownInvokeFailure = {
  signature: string;
  matches: (raw: string) => boolean;
  headline: string;
  steps: readonly string[];
};

/**
 * 화면에 나갈 수 있는 «알려진» 실패 → 한국어 처방.
 *
 * 여기 없는 문자열은 손대지 않아요(`explainInvokeFailure` 의 마지막 분기).
 */
export const KNOWN_INVOKE_FAILURES: readonly KnownInvokeFailure[] = [
  {
    // 근거 ①: `scaffold.py` `message/send` 핸들러가 `모델 호출 실패: {_exception_reason(e)}`
    //         로 감싸고, `_exception_reason` 이 `{예외 타입}: {메시지}` 를 만들어요.
    // 근거 ②: 라이브 관측 — `/aws/bedrock-agentcore/runtimes/cs_bot_ver3-kx0Pz7Ge71-DEFAULT`,
    //         2026-09-06 07:08:46 KST, `message/send failed: MaxTokensReachedException: …`.
    signature: "MaxTokensReachedException",
    matches: (raw) => raw.includes("MaxTokensReachedException"),
    headline:
      "모델이 한 응답에 쓸 수 있는 토큰 상한에 닿아서, 답변이 끝까지 나오지 못하고 멈췄어요.",
    steps: [
      "질문을 더 좁혀서 다시 물어보세요 — 건수를 줄이거나(예: 최근 5건만) 브랜드·카테고리로 걸면 답변이 짧아져요. 대개 이걸로 끝나요.",
      "여기까지 만든 답변은 대화 이력에 이미 들어가 있어요. 「계속해 줘」라고 보내면 뒤를 이어받아요.",
      "그래도 계속 잘리면 Initializr 의 「실행 상한 → max tokens」 를 올려 다시 배포하세요. ⚠️ 재배포는 도구 승인을 초기화할 수 있어서, 배포 뒤 승인 상태를 다시 확인해야 해요.",
    ],
  },
  {
    // 근거 ①: 같은 `모델 호출 실패:` 경로예요. Bedrock `ConverseStream` 이 복원된 대화 이력의
    //         thinking 블록을 거부하면 `ValidationException` 이 그대로 실려 나와요 —
    //         `invoke_service._classify_invoke_error` 는 CreateEvent 413 만 걸러요.
    // 근거 ②: 라이브 관측 — 같은 로그그룹, 2026-09-05 23:49:08 · 23:51:19 KST 2회,
    //         `ValidationException: … messages.3.content.2: \`thinking\` or \`redacted_thinking\`
    //         blocks in the latest assistant message cannot be modified.`
    // 처방 근거: `docs/06-risks.md` IH-174 — 고친 코드는 «생성 코드» 쪽이라 배포된 agent 엔
    //         아직 없고, 방아쇠는 세션 «복원»(microVM 재활용 · 15분 이상 유휴 · 동시 세션
    //         LRU 축출)이에요. 회피는 새로고침 한 번 / 새 세션 / 쉬지 않기예요.
    //         ⚠️ 내부 티켓 번호는 사용자 문구에 넣지 않아요 — 시연 화면이에요.
    signature: "thinking_block_history_rejected",
    matches: (raw) =>
      raw.includes("ValidationException")
      && (raw.includes("redacted_thinking") || raw.includes("thinking")),
    headline:
      "이어받은 대화 이력이 모델의 사고 블록 규칙과 맞지 않아, 이 턴이 모델에서 거부됐어요.",
    steps: [
      "화면을 새로고침한 뒤 한 번 더 보내보세요. 대개 여기서 풀려요.",
      "그래도 같으면 새 대화로 시작하세요 — agent 를 다시 고르면 세션이 새로 발급돼요.",
      "이 오류는 대화를 오래 쉬었다가 다시 물을 때(약 15분 이상) 생겨요. 이어서 물어볼 때는 중간에 오래 쉬지 않는 편이 안전해요.",
    ],
  },
];

/**
 * 실패를 사람 말 + 처방 + 원문으로 갈라요.
 *
 * 서버가 구조화된 `remediation` 을 준 경우(예: 413 `입력이 너무 커서 …`)는 그 문장을 그대로
 * 처방으로 써요 — 그건 **백엔드가 소유한 값** 이라 이 파일이 다시 쓰면 안 돼요.
 */
export function explainInvokeFailure(error: unknown): InvokeFailureExplanation {
  const raw = error instanceof Error ? error.message : "호출 실패";
  const detail =
    error instanceof Error && "detail" in error
      ? (error.detail as ApiErrorDetail | undefined)
      : undefined;
  const remediation = detail?.remediation;
  const serverSteps = remediation ? [remediation] : [];
  const known = KNOWN_INVOKE_FAILURES.find((entry) => entry.matches(raw));
  if (known) {
    return {
      headline: known.headline,
      steps: [...known.steps, ...serverSteps],
      signature: known.signature,
      raw,
    };
  }
  // 모르는 예외 — 관측한 것을 그대로 보여줘요. 여기서 뭉개면 진단 단서가 사라져요.
  return { headline: raw, steps: serverSteps, signature: null, raw };
}

/**
 * 원문을 «따로» 그려야 하는지.
 *
 * 매핑에 걸려 사람 말로 갈아치운 경우에만 참이에요. 모르는 예외는 `headline` 이 곧 원문이라
 * 두 번 그릴 필요가 없어요. 어느 경우든 원문은 화면에서 읽을 수 있어요.
 */
export function invokeFailureShowsRawSeparately(
  explanation: InvokeFailureExplanation,
): boolean {
  return explanation.headline !== explanation.raw;
}

/**
 * 한 문자열로 평평하게 — 활동 트리의 `오류` 상세 행처럼 문자열만 받는 자리에 써요.
 * 원문을 «반드시» 포함해요(트리에서도 원문에 도달할 수 있어야 해요).
 */
export function invokeFailureText(explanation: InvokeFailureExplanation): string {
  const lines = [explanation.headline];
  explanation.steps.forEach((step, index) => lines.push(`${index + 1}. ${step}`));
  if (invokeFailureShowsRawSeparately(explanation)) {
    lines.push(`${INVOKE_FAILURE_RAW_LABEL}: ${explanation.raw}`);
  }
  return lines.join("\n");
}

/** 예전 호출부 호환용 — 문자열 한 덩어리가 필요한 자리에 그대로 쓸 수 있어요. */
export function invokeErrorMessage(error: unknown): string {
  return invokeFailureText(explainInvokeFailure(error));
}
