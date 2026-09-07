/**
 * 활동 트리 — 지금 실제로 관측되는 것만으로 한 턴의 활동을 중첩 노드로 만들어요.
 *
 * **대상은 Runtime(Strands) 경로뿐이에요.** 노드 종류도,
 * 로그 분류기도 Strands + AgentCore Memory 가 내보내는 것만 다뤄요.
 *
 * 시안(`docs/design/playground-activity-tree-mockup.html`)은 지연·토큰·비용·span 계층까지
 * 그리지만, 그 값의 출처는 **OTEL span**(runtime 로그그룹의 `spans`·`otel-rt-logs` 스트림)
 * 이고 아직 Agora 가 읽지 않아요. 그래서 여기서는 만들어 채우지 않고 **미관측으로
 * 표시**해요(ADR-0037 §4).
 *
 * 오늘 관측 가능한 재료는 두 개뿐이에요.
 *
 * 1. **invoke 왕복** — 브라우저가 직접 측정한 포털 왕복 시간과 성공·실패. 모델 지연이
 *    아니라 포털→Runtime 왕복이라 라벨도 그렇게 붙여요.
 * 2. **CloudWatch 런타임 로그 라인** — 배포 컨테이너의 stdout. 두 종류가 섞여 있어요.
 *    - Strands `PrintingCallbackHandler` 의 **print** — `Tool #N: <tool_name>`
 *      (`strands/handlers/callback_handler.py:44`). 레벨·logger 접두어가 없어요. 실제
 *      배포 로그에서 확인했어요(2026-08-22, `Tool #1: weather-mcp___get_today_weather`).
 *      **이게 지금 도구 호출의 유일한 관측 경로예요.**
 *    - logging 출구(`"%(levelname)s %(name)s %(message)s"`, IH-96 로 열림). 배포 코드가
 *      레벨을 올린 logger 만 보여요:
 *      · `strands.event_loop.*` (DEBUG) — `model=<...> | streaming messages` 가 model invoke
 *      · `strands.agent.conversation_manager.*` (DEBUG) — context 관리
 *      · `bedrock_agentcore.memory.integrations.strands` (INFO, MANAGED memory 일 때) —
 *        `Retrieved N customer context items` 가 장기 기억 주입
 *      IH-96 이전에 배포된 agent 는 이 출구가 없어서 logging 줄이 아예 안 나와요 —
 *      그래서 "없음"을 "안 했음"으로 읽지 않게 미관측으로 표시해요.
 *
 * **Cedar 판정·도구 인자·도구 결과는 이 경로로 관측되지 않아요.** 그래서 관측된 tool 노드
 * 밑에 Cedar 판정을 **미관측 노드로 명시**해요 — 노드가 없으면 "판정이 없었다" 로
 * 오독되니까요. 응답 텍스트도 같은 stdout 으로 흘러나와서(같은 print 핸들러) 분류되지
 * 않은 줄로 남아요. DEBUG·INFO와 응답 출력은 활동으로 단정하지 않고 트리에서 제외하며,
 * WARNING·ERROR·CRITICAL만 진단 노드로 유지해요.
 *
 * ## authz 는 축이 두 개예요 (제품 오너 지적, 2026-08-22)
 *
 * 처음엔 authz 노드를 tool 자식으로만 뒀는데, 그러면 "Cedar 판정은 도구 호출의 하위 단계"
 * 처럼 읽혀요. 실제로 Agora 에는 **성격이 다른 두 판정**이 있어요.
 *
 * 1. **invocation 인가 — 턴의 자식.** 포털의 `enforce_agent_invoke_gate`(소유자 ·
 *    principal allowlist · group)가 Runtime 을 부르기 **전에** 내려요. Cedar 가 아니고,
 *    도구를 하나도 안 쓰는 턴에도 있어요. 판정 주인이 우리 백엔드라 통과·거부는 관측된
 *    사실이에요(사유 코드는 응답에 없어 미관측).
 * 2. **도구 인가 — tool 의 자식.** Gateway 가 Cedar 로 내려요. Agora 의 Cedar action 이
 *    **정확히 도구 operation 하나**(`AgentCore::Action::"{target}___{operation}"`,
 *    resource=Gateway, principal=agent — `identity/agent_policy_compiler.py`)라서 판정
 *    하나가 도구 호출 하나에 1:1로 대응해요. 그래서 이 자리에 붙는 게 맞아요.
 *
 * 도구 호출을 관측하지 못한 턴에는 Cedar 노드를 만들지 않아요 — 없는 도구 호출 밑에
 * 판정을 매달면 두 축이 다시 뒤섞여요.
 *
 * ## 도구 호출: 타일은 «자기보고», 트리 노드는 «로그 관측» (2026-09-06 IH-184)
 *
 * 하루 안에 두 번 바뀐 축이라 계보를 적어 둬요.
 *
 * - IH-182 (같은 날 오전): 자기보고 축을 걷고 로그 관측 하나만 남겼어요.
 * - IH-184 (같은 날 밤, 제품 오너 결정): **다시 갈랐는데 자리를 바꿨어요** — Playground 는
 *   「agent 가 말하는 것」, 어드민 감사로그는 「실제 로그」예요.
 *
 * 뒤집은 근거는 같은 날 실측이에요. **로그 관측은 구조적으로 과소집계예요** — ⑴ OTel 로그
 * exporter 가 배치 1,048,576 bytes 를 넘으면 HTTP 400 `Upload too large` 로 **그 배치를
 * 버려요**(22:08·22:15 KST 4건, 1,119,397·1,138,594 bytes 실측) ⑵ 로그 API 가 `since_ms`
 * 앞쪽을 못 봐요. 반면 자기보고는 매 턴 invoke 응답에서 와서 **세션 전체가 남아요.**
 *
 * 그래서 이 화면에서는:
 *
 * - **요약 타일 「도구 호출」 = 자기보고** (`usage.metrics.tool_metrics`).
 * - **트리의 `Tool #N:` 노드 = 로그 관측, 그대로 남겨요.** 지우지 않는 이유가 셋이에요 —
 *   ⒜ Agora 자신이 하는 유일한 «관측» 이에요 ⒝ Cedar 판정 노드가 여기 매달려요(자기보고에
 *   매달면 자기보고가 판정 근거처럼 읽혀요) ⒞ 각 노드가 이미 `출처` 상세 행으로 출처를 밝혀요.
 * - **어긋남은 자기보고 노드의 상세 «한 행»** 으로만 말해요. IH-182 가 지운 상단 배너는
 *   되살리지 않아요(항상 켜진 산문을 뺀 게 그 지시의 취지였어요).
 *
 * ⚠️ **어느 쪽도 «총계» 가 아니에요.** 자기보고는 agent 의 주장이고, 로그 관측은 하한이에요.
 * 그래서 「자기보고 없음」이 0 회로 그려지면 안 돼요 — `activityMetricValue` 가 그 자리예요.
 */

import type { InvocationUsage } from "../api/playground.ts";
/**
 * 출처 라벨 둘은 `./selfReportedTools` 가 함께 소유해요 — 한 파일에 두면 갈라질 수 없어요.
 * IH-182 가 자기보고 모듈을 걷으면서 이 상수를 여기로 옮겼는데, IH-184 로 그 모듈이 돌아와서
 * 원래 자리로 되돌렸어요. 여기서는 **재수출만** 해요(기존 import 경로를 깨지 않으려고요).
 */
export {
  LOG_OBSERVED_TOOLS_SOURCE_LABEL,
  SELF_REPORTED_TOOLS_SOURCE_LABEL,
} from "./selfReportedTools.ts";
import {
  LOG_OBSERVED_TOOLS_SOURCE_LABEL,
  SELF_REPORTED_TOOLS_FOOTNOTE,
  // `SELF_REPORTED_TOOLS_SOURCE_LABEL` 는 여기서 «쓰지» 않아요 — IH-187 으로 노드 라벨과
  // 자리표시자 문장에서 출처 표기를 걷었어요. 위의 재수출은 남겨 둬요(`ActivityTree.tsx` 의
  // 타일 호버 `title` 과 기존 import 경로가 그걸 읽어요).
  selfReportedToolLabel,
  selfReportedTools,
  toolObservationDisagreement,
  type SelfReportedTools,
} from "./selfReportedTools.ts";

export type ActivityKind =
  | "turn"
  | "model"
  | "memory"
  | "context"
  | "tool"
  | "authz"
  | "error"
  | "warning"
  | "app"
  | "log";

export type ActivityNode = {
  id: string;
  kind: ActivityKind;
  label: string;
  /** 배지에 찍히는 짧은 종류 표시. */
  tag: string;
  /** 오른쪽 끝 보조 정보(관측된 값만). */
  meta?: string;
  /** 관측 여부. false 면 화면이 흐리게 + "미관측" 으로 표시해요. */
  observed: boolean;
  status?: "ok" | "error" | "unknown";
  /** 노드를 열면 보이는 상세 행. */
  detail?: { key: string; value: string }[];
  /** 왜 이 값이 없는지 / 이 노드가 무슨 뜻인지. */
  note?: string;
  children: ActivityNode[];
};

/**
 * invocation 단위 인가 판정 — 이 턴을 호출할 자격이 있었는지.
 *
 * **도구별 Cedar 판정과 다른 축이에요.** 이건 포털의 agent-invoke 게이트
 * (`enforce_agent_invoke_gate` — 소유자 · principal allowlist · group)가 Runtime 을 부르기
 * **전에** 내리는 판정이고, 도구를 하나도 안 쓰는 턴에도 있어요. Cedar 는 그 뒤 Gateway 가
 * 도구 호출마다 따로 판정해요.
 */
export type InvokeGateDecision = "allow" | "deny" | "unknown";

/** 한 번의 대화 왕복. 로그 라인은 도착한 순서대로 여기 쌓여요. */
export type ActivityTurn = {
  index: number;
  prompt: string;
  /** 브라우저가 측정한 포털 왕복 ms. 아직 안 끝났으면 null. */
  roundTripMs: number | null;
  state: "running" | "done" | "error";
  errorText?: string;
  invocationId?: string;
  /** invoke 게이트 판정. 아직 모르면 `unknown`. */
  gate?: InvokeGateDecision;
  lines: string[];
  /**
   * invoke 응답이 실어 보낸 **agent 자기보고** 사용량. 로그 관측과 별개 출처예요.
   *
   * 성공 응답에만 있어요 — 실패는 `detail` 만 오는 오류 응답이라 여기 안 담겨요.
   */
  usage?: InvocationUsage;
};

/**
 * HTTP 상태로 invoke 게이트 판정을 읽어요.
 *
 * **200 과 403 만 판정을 증명해요.** 200 이면 게이트를 통과했고, 403 이면 게이트가 막았어요.
 * 그 밖의 상태는 전부 `unknown` 이에요.
 *
 * 처음엔 5xx 를 "게이트 뒤에서 깨진 것" 이라 보고 `allow` 로 뒀는데 **틀렸어요**(codex 리뷰,
 * 2026-08-22). 5xx 는 게이트 **안에서**도 날 수 있어요 — 게이트가 읽는 identity store 가
 * 죽으면 `enforce_agent_invoke_gate` 자체가 500 으로 터지고, BFF·프록시 단계의 502·503 은
 * 백엔드에 닿기도 전이에요. 그걸 `allow` 로 표시하면 **관측 불가를 통과로 보여주는**
 * ADR-0037 §4 위반이에요.
 *
 * 대가로 502 로 실패한 턴은 게이트가 미관측으로 남아요. 그걸 되찾으려면 백엔드가 판정을
 * 응답으로 **직접 알려줘야** 해요(추론이 아니라 보고) — 후속으로 분리했어요.
 */
export function gateFromStatus(status: number | null): InvokeGateDecision {
  if (status === null) return "allow"; // 200 — 게이트를 통과했어요.
  if (status === 403) return "deny";
  return "unknown";
}

const LEVELS = new Set(["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]);

/**
 * 줄 **중간**에 붙어버린 로그 레코드를 찾는 패턴.
 *
 * 왜 필요한가: `PrintingCallbackHandler` 가 응답 텍스트를 `print(data, end="")` 로 내보내서
 * 줄바꿈이 없어요. 그래서 다음에 찍히는 로그 레코드가 응답 텍스트 뒤에 그대로 붙은 채
 * CloudWatch 이벤트 하나가 돼요 — 실측(2026-08-22):
 * `천만에요! 😊DEBUG strands.event_loop._retry stop_reason=<end_turn> | ...`.
 * 앞에서만 파싱하면 이런 줄의 로그 정보를 통째로 잃어요.
 *
 * 오탐을 막으려고 logger 이름을 **알려진 최상위 네임스페이스로 제한**해요
 * (`strands`·`bedrock_agentcore`·`agent`·`main`·`uvicorn` + 점 경로). 처음엔 "점이 하나라도
 * 있는 이름" 이면 다 받았는데, 응답 텍스트가 같은 stdout 으로 나오기 때문에 **사용자가
 * 채팅에 로그처럼 생긴 문장을 쓰면 그게 진짜 활동 노드로 분류됐어요**(codex 리뷰 재현,
 * 2026-08-22: 사용자가 `DEBUG strands.event_loop.streaming model=fake | streaming messages`
 * 라고 설명하면 model.invoke 노드가 생김).
 *
 * ⚠️ **완전한 방어는 아니에요.** allowlist 안의 이름을 그대로 쓰면 여전히 통과해요. 근본
 * 해결은 응답 텍스트와 로그 레코드를 애초에 섞지 않는 것(IH-103, 생성 코드의 print 규약)
 * 이에요. 그때까지는 이 분류를 **참고 수준**으로 읽어야 해요.
 */
const EMBEDDED_RECORD =
  /(DEBUG|INFO|WARNING|ERROR|CRITICAL) ((?:strands|bedrock_agentcore|agent|main|uvicorn)(?:\.[A-Za-z0-9_]+)+) /;

/**
 * **라이브(OTel 자동계측) 포맷.** 지금 배포된 agent 가 실제로 내는 모양이에요.
 *
 * ⚠️ 이걸 놓쳐서 `model 호출` · `memory` 카운터가 **구조적으로 항상 0** 이었어요
 * (실측 2026-09-06, `cs_bot_ver3-kx0Pz7Ge71-DEFAULT`). 생성 코드는
 * `"%(levelname)s %(name)s %(message)s"` 를 기대하는데, OTel 자동계측이 root 포맷터를
 * 갈아치워서 라이브 줄은 이렇게 와요:
 *
 * ```
 * 2026-09-05 23:19:32,574 INFO [bedrock_agentcore.memory.integrations.strands.session_manager] [session_manager.py:917] [trace_id=… span_id=… resource.service.name=… trace_sampled=True] - Retrieved 5 customer context items
 * ```
 *
 * 앞의 두 경로가 둘 다 실패했어요 — 첫 토큰이 `2026-09-05` 라서 레벨 판정이 죽고,
 * `EMBEDDED_RECORD` 는 레벨 뒤에 logger 가 **바로** 오길 기대하는데 라이브는 대괄호가 끼어
 * 있어요. `parsed` 가 `null` 이면 `logger = ""` 라 memory·model 분기를 절대 못 타고 마지막
 * fallback 인 `kind: "log"` 로 떨어지고, `buildTurnNode` 의 `if (c.kind !== "log")` 가
 * 그 노드를 트리에서 빼요. `Tool #N:` 만 살아남은 이유도 이걸로 설명돼요 — 그건 `print()`
 * 라 접두어가 없어서 `TOOL_CALL` 정규식이 그대로 맞아요.
 *
 * **logger allowlist 를 안 쓰는 이유:** allowlist 는 사용자가 채팅에 로그처럼 생긴 문장을
 * 쓰는 오탐을 막으려고 붙였어요(`EMBEDDED_RECORD` 주석). 이 패턴은 `YYYY-MM-DD
 * HH:MM:SS,mmm` 전체와 `[logger]` 대괄호, 그리고 ` - ` 구분자까지 한꺼번에 요구해서 사람이
 * 우연히 쓸 문장이 아니에요. 대신 그 덕에 `httpx`·`botocore.credentials`·
 * `mcp.client.streamable_http` 처럼 점이 없거나 allowlist 밖인 logger 도 읽혀요(대부분
 * `kind: "log"` 로 떨어져 트리에는 안 들어가요).
 *
 * 응답 텍스트에 **붙어버린** 경우도 있으니(`PrintingCallbackHandler` 의 개행 없는 print)
 * 줄 맨 앞으로 앵커하지 않고 어디서든 찾아요.
 */
const OTEL_RECORD =
  /(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) (DEBUG|INFO|WARNING|ERROR|CRITICAL) \[([A-Za-z_][A-Za-z0-9_.]*)\]((?: \[[^\]\n]*\])*) - /;

export type ParsedLine = {
  level: string;
  logger: string;
  message: string;
  /**
   * 라이브 포맷에서 잘라낸 부속 정보(타임스탬프·소스 위치·trace_id 블록).
   *
   * 잘라내는 이유는 분류 정규식 중 `startsWith` 를 쓰는 것들(`Created session:` 등)이
   * 접두어 때문에 안 맞기 때문이에요. **버리지 않고 여기 담아** 상세 행으로 다시 보여줘요.
   * 옛 포맷에는 이 키가 아예 없어요.
   */
  context?: string;
};

/**
 * 배포 로그 한 줄을 `level logger message` 로 쪼개요. 포맷이 다르면(uvicorn access 등)
 * `null` — 추측해서 억지로 분류하지 않아요.
 *
 * 세 포맷을 다 읽어요: 라이브 OTel 포맷 → 응답 텍스트에 붙은 옛 레코드 → 줄 맨 앞의 옛
 * 레코드. **옛 포맷을 계속 읽어야 해요** — 다른 시점에 배포된 runtime 이 남아 있어요.
 */
export function parseLogLine(line: string): ParsedLine | null {
  const otel = parseOtelRecord(line);
  if (otel) return otel;
  const embedded = parseEmbeddedRecord(line);
  if (embedded) return embedded;
  const trimmed = line.trimStart();
  const first = trimmed.indexOf(" ");
  if (first <= 0) return null;
  const level = trimmed.slice(0, first);
  if (!LEVELS.has(level)) return null;
  const rest = trimmed.slice(first + 1).trimStart();
  const second = rest.indexOf(" ");
  if (second <= 0) return null;
  const logger = rest.slice(0, second);
  // logger 이름은 점으로 구분된 모듈 경로예요. 아니면 우리 포맷이 아니에요.
  if (!/^[A-Za-z_][A-Za-z0-9_.]*$/.test(logger)) return null;
  return { level, logger, message: rest.slice(second + 1) };
}

function parseOtelRecord(line: string): ParsedLine | null {
  const match = OTEL_RECORD.exec(line);
  if (match === null) return null;
  const brackets = match[4].trim();
  return {
    level: match[2],
    logger: match[3],
    message: line.slice(match.index + match[0].length),
    context: brackets ? `${match[1]} ${brackets}` : match[1],
  };
}

function parseEmbeddedRecord(line: string): ParsedLine | null {
  const match = EMBEDDED_RECORD.exec(line);
  // index 0 이면 줄 맨 앞이라 일반 경로가 처리해요.
  if (match === null || match.index === 0) return null;
  return {
    level: match[1],
    logger: match[2],
    message: line.slice(match.index + match[0].length),
  };
}

export type Classified = {
  kind: ActivityKind;
  label: string;
  tag: string;
  meta?: string;
  status?: "ok" | "error" | "unknown";
  note?: string;
  detail?: { key: string; value: string }[];
};

/** Strands 세션 매니저 — 이력 «복원·등록» 쪽 줄을 내요. */
const MEMORY_LOGGER = "bedrock_agentcore.memory.integrations.strands";
/**
 * Memory 데이터플레인 클라이언트 — 실제 «쓰기·조회» 줄을 내요.
 *
 * ⚠️ 이 logger 가 목록에 없어서 **쓰기를 아무도 안 보고 있었어요.** 라이브 6시간 창에서
 * `Created event:` 가 206건 찍히는데 화면 memory 카운터는 0 이었어요(실측 2026-09-06).
 * 읽기(`session_manager`)와 쓰기(`memory.client`)는 라벨을 갈라요 — 한 이름으로 묶으면
 * 「대화가 저장됐다」와 「이력을 꺼냈다」가 한 숫자에 섞여요.
 */
const MEMORY_CLIENT_LOGGER = "bedrock_agentcore.memory.client";

/**
 * 로그 한 줄을 활동 노드 후보로 분류해요. 분류 근거는 각 라이브러리의 실제 로그
 * 문자열이에요(strands `event_loop/streaming.py`, bedrock_agentcore
 * `memory/integrations/strands/session_manager.py`).
 */
/** Strands PrintingCallbackHandler 의 도구 호출 표시. logger 접두어 없는 raw print 예요. */
const TOOL_CALL = /^Tool #(\d+):\s*(\S+)/;

/**
 * 상세 카드에 그릴 로그 행.
 *
 * 라이브 포맷은 분류를 위해 타임스탬프·소스 위치·trace_id 를 `message` 에서 떼어내요. 그
 * 정보는 진단에 쓰이니 **버리지 않고** 별도 행으로 되돌려 놔요.
 */
function logRows(
  message: string,
  parsed: ParsedLine | null,
): { key: string; value: string }[] {
  const rows = [{ key: "로그", value: message }];
  if (parsed?.context) rows.push({ key: "로그 좌표", value: parsed.context });
  return rows;
}

export function classifyLogLine(line: string): Classified {
  const toolCall = TOOL_CALL.exec(line.trim());
  if (toolCall) {
    return {
      kind: "tool",
      label: toolCall[2],
      tag: "TOOL",
      meta: `#${toolCall[1]}`,
      status: "ok",
      // ⚠️ 반복 설명 산문은 뺐어요(제품 오너 결정, 2026-09-06 — 행마다 같은 문장이 되풀이됐어요).
      // 그 자리에는 «데이터» 만 둬요: 어떤 도구를 몇 번째로 불렀나 + 출처.
      // 지운 문장이 담고 있던 사실 둘은 다른 곳에 살아 있어요 — 「인자·결과 미관측」은 아래
      // Cedar 노드의 `기록 위치` 행과 이 모듈 머리말에, 「출처가 로그 관측」은 아래 `출처` 행에요.
      detail: [
        { key: "출처", value: LOG_OBSERVED_TOOLS_SOURCE_LABEL },
        { key: "호출", value: `${toolCall[2]} · 이 턴의 ${toolCall[1]}번째 도구 호출` },
        { key: "로그", value: line.trim() },
      ],
    };
  }

  const parsed = parseLogLine(line);
  const message = parsed ? parsed.message : line;
  const logger = parsed?.logger ?? "";
  const level = parsed?.level ?? "";

  if (logger.startsWith("strands.event_loop") && message.includes("| streaming messages")) {
    return {
      kind: "model",
      label: "model.invoke",
      tag: "MODEL",
      status: "ok",
      note:
        "모델 호출이 시작된 건 관측했어요. 이 로그에는 모델 ID·토큰·지연이 없어서 " +
        "그 값들은 미관측이에요(모델은 선언값을 구성 패널에서 보세요).",
      detail: logRows(message, parsed),
    };
  }

  if (logger.startsWith(MEMORY_LOGGER)) {
    const retrieved = /Retrieved\s+(\d+)\s+customer context items/.exec(message);
    if (retrieved) {
      return {
        kind: "memory",
        label: "memory · 장기 기억 주입",
        tag: "MEMORY",
        meta: `${retrieved[1]}건`,
        status: "ok",
        note:
          "AgentCore Memory 에서 꺼낸 컨텍스트가 이 턴 프롬프트에 들어갔어요. "
          // SDK 구현 확인(`bedrock-agentcore==1.22.0`
          // `memory/integrations/strands/session_manager.py:911-917`): 이 `logger.info` 는
          // `if all_context:` **안에** 있어요. 그래서 **0건 검색은 로그를 아예 안 남겨요** —
          // 줄이 없는 것을 「주입 0건」으로 읽으면 안 돼요.
          + "줄이 없는 턴은 「주입 0건」일 수도, 「주입을 관측하지 못한 것」일 수도 있어요 — "
          + "이 로그만으로는 구분되지 않아요.",
        detail: logRows(message, parsed),
      };
    }
    if (message.startsWith("Created session:")) {
      return {
        kind: "memory",
        label: "memory · 세션 생성",
        tag: "MEMORY",
        status: "ok",
        detail: logRows(message, parsed),
      };
    }
    if (message.startsWith("Created agent:")) {
      return {
        kind: "memory",
        label: "memory · 세션에 agent 등록",
        tag: "MEMORY",
        status: "ok",
        // ⚠️ 예전 라벨은 「memory · 대화 이벤트 기록」이었고 **사실이 아니었어요.** 이 줄은
        // 세션에 agent 상태를 등록한 기록이에요 — 대화 메시지가 적혔다는 뜻이 아니에요.
        //
        // 근거 ① (SDK 구현, `bedrock-agentcore==1.22.0`
        // `memory/integrations/strands/session_manager.py:417-431`): payload 가
        // `json.dumps(session_agent.to_dict())` 이고 metadata 가 `STATE_TYPE_KEY:
        // StateType.AGENT.value` 예요. 대화 메시지가 아니라 **agent 상태 blob** 이에요.
        // 근거 ② (라이브): 메시지를 하나도 보내지 않는 `agora/selfcheck` 세션에서도 찍혀요
        // (2026-09-05 23:18:54, 세션 `agoraselfchecked…`).
        //
        // 이 경로는 `gmdp_client.create_event` 를 **직접** 불러서 `memory.client` 의 래퍼를
        // 건너뛰어요 — 그래서 아래 `Created event:` 와 개수가 1:1이 아니에요(라이브에서도
        // 23:19:38 에 event 2건 / agent 1건이었어요).
        note:
          "세션에 agent 를 등록한 기록이에요. 대화 메시지가 저장됐다는 뜻은 아니에요 — "
          + "메시지가 없는 selfcheck 세션에서도 찍혀요.",
        detail: logRows(message, parsed),
      };
    }
    return {
      kind: "memory",
      label: "memory",
      tag: "MEMORY",
      status: level === "ERROR" ? "error" : "ok",
      detail: logRows(message, parsed),
    };
  }

  if (logger.startsWith(MEMORY_CLIENT_LOGGER)) {
    if (message.startsWith("Created event:")) {
      return {
        kind: "memory",
        label: "memory · 이벤트 기록 (쓰기)",
        tag: "MEMORY",
        status: level === "ERROR" ? "error" : "ok",
        // 「무엇이 담겼는지 미관측」은 과한 조심이 아니라 **정확한 진술**이에요. SDK 구현
        // 확인(`bedrock-agentcore==1.22.0`): 똑같은 `Created event: %s` 문자열을 서로 다른
        // 두 메서드가 내요 — 임의 payload 를 받는 범용 `create_event`(`memory/client.py:555`)
        // 와 `save_conversation`(`:717`). 그래서 이 줄만 보고 대화가 저장됐다고 말할 수 없어요.
        note:
          "AgentCore Memory 데이터플레인에 이벤트가 하나 적혔어요. 무엇이 담겼는지는 이 줄에 "
          + "없어서 미관측이에요.",
        detail: logRows(message, parsed),
      };
    }
    const events = /Retrieved total of\s+(\d+)\s+events/.exec(message);
    if (events) {
      return {
        kind: "memory",
        label: "memory · 이력 조회 (읽기)",
        tag: "MEMORY",
        meta: `${events[1]}건`,
        status: "ok",
        // 위의 장기 기억 주입과 달리 이 줄은 0건에도 찍혀요 — 그래서 `0건` 은 관측된 사실이에요.
        // SDK 구현 확인(`bedrock-agentcore==1.22.0` `memory/client.py:903`): 페이지네이션
        // 루프가 끝난 뒤 **조건 없이** 찍혀요. 두 줄의 「0」이 뜻이 다른 이유예요.
        note: "세션 이력을 몇 건 읽었는지예요. 0건도 이 줄이 남으니 관측된 값이에요.",
        detail: logRows(message, parsed),
      };
    }
    return {
      kind: "memory",
      label: "memory · 클라이언트",
      tag: "MEMORY",
      status: level === "ERROR" ? "error" : "ok",
      detail: logRows(message, parsed),
    };
  }

  if (logger.startsWith("strands.agent.conversation_manager")) {
    return {
      kind: "context",
      label: "context 관리",
      tag: "CONTEXT",
      status: "ok",
      note: "대화 이력을 줄이거나 요약하는 conversation manager 동작이에요.",
      detail: logRows(message, parsed),
    };
  }

  if (message.includes("event loop cycle failed")) {
    return {
      kind: "error",
      label: "event loop 사이클 실패",
      tag: "ERROR",
      status: "error",
      detail: logRows(message, parsed),
    };
  }

  if (level === "ERROR" || level === "CRITICAL") {
    return {
      kind: "error",
      label: logger || "런타임 오류",
      tag: "ERROR",
      status: "error",
      detail: logRows(message, parsed),
    };
  }

  if (level === "WARNING") {
    return {
      kind: "warning",
      label: logger || "런타임 경고",
      tag: "WARN",
      status: "unknown",
      detail: logRows(message, parsed),
    };
  }

  return {
    kind: "log",
    label: logger || "런타임 로그",
    tag: "LOG",
    detail: logRows(message, parsed),
  };
}

/**
 * tool 노드 밑에 매다는 Cedar 판정 노드.
 *
 * 여기 붙는 이유: Agora 의 Cedar action 이 **정확히 도구 operation 하나**예요
 * (`AgentCore::Action::"{gateway_target}___{operation}"`, resource=Gateway, principal=agent —
 * `identity/agent_policy_compiler.py`). 그래서 판정 하나가 도구 호출 하나에 1:1로 대응하고,
 * 강제 지점도 그 호출을 받는 Gateway 예요. "이 도구 호출이 왜 허용/거부됐나" 가 그 도구
 * 밑에 붙는 게 맞아요.
 *
 * 다만 값은 아직 미관측이에요 — 판정은 Gateway 가 내리고 Playground 로그 경로에 안 실려요.
 *
 * ⚠️ **장문 설명은 화면에서 뺐어요**(제품 오너 결정, 2026-09-06 — 도구 호출마다 같은 산문이
 * 되풀이됐어요). 그 문장이 주장하던 사실 셋의 처리는 이래요.
 *
 * 1. 「Cedar action 이 도구 operation 과 1:1」 — 이 주석과 모듈 머리말에 남아 있어요.
 * 2. 「판정은 Gateway 가 내려서 로그에 안 실려요 / 기록은 admin 감사 조회에 남아요」 —
 *    **데이터 행으로 옮겼어요**(`기록 위치`). 이걸 지우면 「어디서 볼 수 있나」가 사라져요.
 * 3. 「턴 전체 자격 판정은 위의 invoke 게이트」 — 두 노드의 **라벨 자체**가 이미 갈라요
 *    (`authz · Cedar (Gateway)` vs `authz · invoke 게이트`).
 */
function cedarPlaceholder(id: string, toolLabel: string): ActivityNode {
  return {
    id: `${id}-authz`,
    kind: "authz",
    label: "authz · Cedar (Gateway)",
    tag: "AUTHZ",
    meta: "미관측",
    observed: false,
    status: "unknown",
    detail: [
      { key: "Cedar action", value: toolLabel },
      { key: "기록 위치", value: "admin 감사 조회" },
    ],
    children: [],
  };
}

/**
 * 턴 노드 밑에 매다는 invocation 단위 인가 노드.
 *
 * Cedar 와 **다른 축**이에요 — 포털의 agent-invoke 게이트(소유자·allowlist·group)가
 * Runtime 호출 전에 내리는 판정이고, 도구를 안 쓰는 턴에도 있어요. 판정 주인이 우리
 * 백엔드라서 통과·거부는 관측된 사실이에요(사유 코드는 응답에 없어 미관측).
 *
 * ⚠️ **클릭하면 열리던 설명 박스를 없앴어요**(제품 오너 결정, 2026-09-06). `detail` 과 `note`
 * 를 둘 다 비워야 `ActivityTree` 의 `hasDetail` 이 거짓이 되어 박스가 «실제로» 안 열려요 —
 * 한쪽만 비우면 박스는 그대로 남아요. 지운 문구가 담고 있던 사실 셋의 처리는 이래요.
 *
 * 1. **통과·거부 상태** — 정보라서 `meta`(ALLOW·DENY·미관측)로 남겼어요. 판정을 못 봤을 때
 *    `observed: false` + `미관측` 이라 **미관측이 통과로 바뀌지 않아요.**
 * 2. 「판정 주인이 Cedar 가 아니다」 — 두 노드의 **라벨**이 이미 갈라요
 *    (`authz · invoke 게이트` vs `authz · Cedar (Gateway)`). 모듈 머리말에도 있어요.
 * 3. 「사유 코드 미관측(OWNER·PRINCIPAL_ALLOWED·GROUP_ALLOWED 구분 없음)」 — 우리가 **주장을
 *    줄인** 거예요. 화면은 이제 사유를 아예 말하지 않으니 없는 값을 통과로 그릴 여지도 없어요.
 */
function invokeGateNode(id: string, turn: ActivityTurn): ActivityNode {
  const gate = turn.gate ?? "unknown";
  const observed = gate !== "unknown";
  return {
    id: `${id}-gate`,
    kind: "authz",
    label: "authz · invoke 게이트",
    tag: "AUTHZ",
    meta: gate === "allow" ? "ALLOW" : gate === "deny" ? "DENY" : "미관측",
    observed,
    status: gate === "allow" ? "ok" : gate === "deny" ? "error" : "unknown",
    children: [],
  };
}

/**
 * 도구 호출을 하나도 «로그로» 관측하지 못한 턴에 붙이는 미관측 노드.
 *
 * ⚠️ 기존 미관측 고지 문구는 그대로 둬요 — 「안 썼다는 뜻일 수도 있고 로그가 아직 도착하지
 * 않았다는 뜻일 수도 있다」는 여전히 참이에요. 로그 API 가 `since_ms` 로 «그 시점 이후» 만
 * 가져와서(`playground/router.py` 의 logs 엔드포인트) 폴링 시작보다 앞선 턴의 줄은 영구히
 * 못 봐요.
 *
 * ⚠️ 자기보고를 «덧붙이던» 문장을 2026-09-06 에 걷었어요(제품 오너 결정 — 로그만 남겨요).
 * 그래서 이 노드의 미관측 고지가 **그 구멍을 알리는 유일한 자리** 가 됐어요. 지우지 마세요.
 */
function toolPlaceholder(
  id: string,
  declaredToolCount: number,
  reported: SelfReportedTools | null,
): ActivityNode {
  const base =
    declaredToolCount > 0
      ? `이 agent 에는 도구가 ${declaredToolCount}개 선언돼 있어요. 이 턴에서는 도구 호출을 ` +
        "관측하지 못했는데, 안 썼다는 뜻일 수도 있고 로그가 아직 도착하지 않았다는 뜻일 " +
        "수도 있어요. 선언·등록 여부는 위 실행 구성 패널에서 확인해요."
      : "이 agent 에는 선언된 도구가 없어요. 도구 호출 관측도 없어요.";
  // 로그로 못 봤어도 agent 가 보고했으면 그 사실을 말해요 — 「관측 못 함」과 「안 씀」을
  // 가르는 유일한 단서예요. 두 값을 한 숫자로 합치지는 않아요.
  //
  // ⚠️ 출처 이름(`agent 자기보고`)을 문장에서 뺐어요 (IH-187). 대신 **어디를 보라고**
  // 가리켜요 — 옆의 「도구 호출」 노드가 그 값의 주인이고, 그 노드의 상세 `출처` 행이 출처를
  // 밝혀요. 같은 사실을 두 노드에서 되풀이하지 않으려는 정리예요.
  const appended =
    reported?.kind === "tools"
      ? ` 이 턴에는 agent 가 보고한 도구 호출이 ${reported.callCount}개 있어요` +
        `(${reported.tools.map(selfReportedToolLabel).join(", ")}) — 아래 「도구 호출」 노드의 ` +
        "상세에서 출처를 확인해요."
      : "";
  return {
    id: `${id}-tool`,
    kind: "tool",
    // 로그 관측 축이라는 걸 라벨로 밝혀요. 옆의 자기보고 노드가 같은 `도구 호출` 이름을 쓰기
    // 때문에, 둘이 나란히 뜨는 턴에서 이름이 겹치면 어느 축인지 화면에서 구분되지 않아요.
    label: `도구 호출 · ${LOG_OBSERVED_TOOLS_SOURCE_LABEL}`,
    tag: "TOOL",
    meta: "미관측",
    observed: false,
    status: "unknown",
    note: base + appended,
    // 도구 호출을 관측하지 못했으면 Cedar 판정 노드도 붙이지 않아요 — 없는 도구 호출 밑에
    // 판정을 매달면 "Cedar 는 도구 호출의 하위 단계" 로 잘못 읽혀요.
    children: [],
  };
}

/**
 * 턴 밑에 «따로» 매다는 agent 자기보고 도구 노드 (IH-184 로 복원).
 *
 * 로그 관측 노드와 **합치지 않아요.** 성질이 달라요 — 로그는 Agora 가 CloudWatch 에서 읽은
 * 관측이고, 이건 agent 가 응답에 실어 보낸 자기보고예요(`usage.metrics.tool_metrics`,
 * `source=agent_report`). 감사 화면과 같은 문구 규약을 써요.
 *
 * 어긋남은 **이 노드의 상세 한 행**으로만 말해요. IH-182 가 지운 상단 배너는 되살리지
 * 않아요 — 항상 켜진 산문을 뺀 것이 그 지시의 취지였어요.
 */
function selfReportNode(
  id: string,
  reported: SelfReportedTools,
  logObservedCalls: number,
): ActivityNode {
  const reportPresent = reported.kind !== "not_reported";
  const detail: { key: string; value: string }[] = [
    { key: "출처", value: SELF_REPORTED_TOOLS_FOOTNOTE },
  ];
  for (const tool of reported.tools) {
    detail.push({
      key: tool.toolName,
      value:
        `${tool.callCount}회 (성공 ${tool.successCount} · 실패 ${tool.errorCount}) · `
        + tool.fullName,
    });
  }
  if (reported.discardedCount > 0) {
    detail.push({
      key: "서버가 버린 이름",
      value: `${reported.discardedCount}개 — 이름 규칙·Registry 선언 대조에서 걸렀어요.`,
    });
  }
  const disagreement = toolObservationDisagreement(logObservedCalls, reported);
  if (disagreement) {
    detail.push({ key: "출처 불일치", value: disagreement });
  }
  return {
    id: `${id}-self-report`,
    kind: "tool",
    // ⚠️ 라벨에서 출처 표기를 뺐어요 (제품 오너 결정, 2026-09-07 IH-187). 어제 IH-184 는
    // `도구 호출(agent 자기보고)` 로 적었는데, 행마다 출처가 되풀이돼 읽기 어려웠어요.
    //
    // **두 축을 합친 게 아니에요.** 출처는 상세 `출처` 행에 그대로 있고(위 `detail` 의 첫
    // 행), 로그 관측 축은 별 노드로 남아 있어요. 두 축을 가르는 «식별» 은 이제 라벨이 아니라
    // 노드 id (`-self-report`) 예요 — 테스트도 id 로 찾아요. 라벨로 찾으면 문구를 다듬을
    // 때마다 규율 테스트가 같이 흔들려요.
    label: "도구 호출",
    tag: "TOOL",
    meta: reportPresent ? `${reported.callCount}회` : reported.label,
    observed: reportPresent,
    status: reportPresent ? "ok" : "unknown",
    note: reported.detail,
    detail,
    // Cedar 판정 노드는 **로그 관측 노드** 밑에만 매달아요 — 자기보고에 매달면 자기보고가
    // 판정의 근거처럼 읽혀요.
    children: [],
  };
}


/** 한 턴을 트리 노드로 조립해요. */
export function buildTurnNode(
  turn: ActivityTurn,
  options: { declaredToolCount: number },
): ActivityNode {
  const id = `turn-${turn.index}`;
  // invocation 인가가 먼저예요 — 시간 순서로도, 읽는 순서로도 도구보다 앞이에요.
  const children: ActivityNode[] = [invokeGateNode(id, turn)];
  let modelSeq = 0;
  let observedTools = 0;

  turn.lines.forEach((line, i) => {
    const c = classifyLogLine(line);
    if (c.kind === "model") modelSeq += 1;
    if (c.kind === "tool") observedTools += 1;
    const nodeId = `${id}-l${i}`;
    const node: ActivityNode = {
      id: nodeId,
      kind: c.kind,
      label: c.kind === "model" ? `${c.label} · 턴 내 ${modelSeq}회차` : c.label,
      tag: c.tag,
      meta: c.meta,
      observed: true,
      status: c.status,
      detail: c.detail,
      note: c.note,
      // 관측된 도구 호출에는 Cedar 판정 노드를 매달아요 — Cedar action 이 그 도구
      // operation 과 1:1이라 이 자리가 맞고, 값이 미관측이라는 사실을 그 자리에서 말해요.
      children: c.kind === "tool" ? [cedarPlaceholder(nodeId, c.label)] : [],
    };
    if (c.kind !== "log") {
      children.push(node);
    }
  });

  // 진행 중인 턴은 아직 응답이 없어서 자기보고를 «없다» 고 말할 수 없어요.
  const reported =
    turn.state === "running"
      ? null
      : selfReportedTools(turn.usage, { turnFailed: turn.state === "error" });

  if (observedTools === 0) {
    children.push(toolPlaceholder(id, options.declaredToolCount, reported));
  }
  if (reported) {
    children.push(selfReportNode(id, reported, observedTools));
  }

  // ⚠️ 「토큰·비용·모델 지연」 행을 뺐어요(제품 오너 결정, 2026-09-06). 값이 될 수 없는
  // 칸이라 라이브에서 「미관측 — OTEL span 을 읽지 않아요」만 계속 떴어요.
  //
  // **칸을 지운 것과 0 으로 채운 것은 다른 사실이에요.** 지운 건 「제공하지 않는다」이고,
  // 0 은 「관측했고 0 이다」예요. 그래서 지우는 쪽만 했고 어떤 값도 만들어 넣지 않았어요.
  //
  // 근거 사실은 여기 주석으로 남겨요: 토큰·비용·모델 지연·span 계층의 출처는 runtime
  // 로그그룹의 **OTEL span**(`spans`·`otel-rt-logs` 스트림)이고 Agora 는 아직 읽지 않아요.
  // 도구 응답 «본문»(`body.content[].toolResult.content[].text`)도 같은 span 안에 있어요 —
  // span body 하나가 34~72KB 이고 주입된 장기 기억(고객 주소·사용자 ID)까지 들어 있어서
  // 읽기 경로·크기 제한·PII 판단이 함께 필요해요(별 티켓, 2026-09-06 실측).
  //
  // 「포털 왕복」은 **남겨요** — 브라우저가 직접 측정한 실측값이고 모델 지연과 다른 사실이에요.
  const detail: { key: string; value: string }[] = [
    { key: "질문", value: turn.prompt },
    {
      key: "포털 왕복",
      // 모델 지연이 아니라 브라우저→포털→Runtime 왕복이에요. 라벨로 구분해요.
      value:
        turn.roundTripMs === null
          ? "진행 중"
          : `${(turn.roundTripMs / 1000).toFixed(1)}s (클라이언트 측정)`,
    },
  ];
  if (turn.invocationId) detail.push({ key: "invocation", value: turn.invocationId });
  if (turn.errorText) detail.push({ key: "오류", value: turn.errorText });

  return {
    id,
    kind: "turn",
    label: `대화 턴 ${turn.index}`,
    tag: "TURN",
    meta:
      turn.state === "running"
        ? "진행 중"
        : turn.roundTripMs === null
          ? undefined
          : `${(turn.roundTripMs / 1000).toFixed(1)}s`,
    observed: true,
    status: turn.state === "error" ? "error" : turn.state === "done" ? "ok" : "unknown",
    detail,
    children,
  };
}

export function buildActivityTree(
  turns: ActivityTurn[],
  options: { declaredToolCount: number },
): ActivityNode[] {
  return turns.map((turn) => buildTurnNode(turn, options));
}

/** 트리 헤더의 요약 — 관측된 것만 셈해요. */
export type ActivitySummary = {
  turnCount: number;
  modelInvokes: number;
  /** **런타임 로그로 관측한** 도구 호출 수. 자기보고와 합치지 않아요. */
  toolCalls: number;
  /**
   * agent 자기보고 도구 호출 합 (IH-184). `toolCalls` 와 **합치지 않아요** — 출처가 달라요.
   * 요약 타일이 읽는 값이에요. 로그 관측은 하한이고, 이 값은 세션 전체가 남지만 **주장**이에요.
   */
  selfReportedToolCalls: number;
  /**
   * 자기보고가 **하나라도 있었나.** 없으면 `selfReportedToolCalls` 가 0 이어도 숫자로 쓰면
   * 안 돼요 — 「보고 안 함」과 「0회 호출」은 다른 사실이에요.
   */
  selfReportedAvailable: boolean;
  memoryHits: number;
  errors: number;
};

export function activityMetricValue(
  value: number,
  sourceObservable: boolean,
): number | string {
  return sourceObservable || value > 0 ? value : "–";
}

export type RuntimeLogPollingState = {
  ready: boolean;
  enabled: boolean;
  status: string;
  error: string;
  permanentlyStopped: boolean;
};

export type RuntimeLogPollingPresentation = {
  active: boolean;
  badge: string;
  hint: string;
};

export function runtimeLogPollingPresentation(
  state: RuntimeLogPollingState,
): RuntimeLogPollingPresentation {
  if (!state.ready) {
    return {
      active: false,
      badge: "대기",
      hint: "agent를 먼저 선택해 주세요.",
    };
  }
  if (state.permanentlyStopped) {
    return {
      active: false,
      badge: "관측 중단",
      hint: "로그 관측이 중단돼 새 활동을 추가할 수 없어요.",
    };
  }
  if (!state.enabled) {
    return {
      active: false,
      badge: "대기",
      hint: "관측이 멈춰 있어요. 아래 '관측 재개'를 눌러 주세요.",
    };
  }
  if (state.error || state.status === "unavailable") {
    return {
      active: false,
      badge: "재시도 중",
      hint: "로그 조회 실패로 새 활동을 관측하지 못했어요. 자동으로 다시 시도해요.",
    };
  }
  if (state.status === "not_deployed") {
    return {
      active: false,
      badge: "관측 불가",
      hint: "배포된 runtime이 없어 로그 활동을 관측할 수 없어요.",
    };
  }
  if (state.status === "ok" || state.status === "empty") {
    return {
      active: true,
      badge: "로그 관측 중",
      hint: "런타임 로그를 4초마다 읽어 관측한 활동만 트리로 만들어요.",
    };
  }
  return {
    active: true,
    badge: "확인 중",
    hint: "런타임 로그를 확인하는 중이에요.",
  };
}

export function activitySummary(turns: ActivityTurn[]): ActivitySummary {
  let modelInvokes = 0;
  let toolCalls = 0;
  let selfReportedToolCalls = 0;
  let selfReportedAvailable = false;
  let memoryHits = 0;
  let errors = 0;
  for (const turn of turns) {
    for (const line of turn.lines) {
      const kind = classifyLogLine(line).kind;
      if (kind === "model") modelInvokes += 1;
      else if (kind === "tool") toolCalls += 1;
      else if (kind === "memory") memoryHits += 1;
      else if (kind === "error") errors += 1;
    }
    if (turn.state === "error") errors += 1;
    // 진행 중인 턴은 응답이 없어서 「보고 안 함」이라고 말할 수 없어요 — 세지 않고 넘어가요.
    if (turn.state !== "running") {
      const reported = selfReportedTools(turn.usage, {
        turnFailed: turn.state === "error",
      });
      if (reported.kind !== "not_reported") {
        selfReportedAvailable = true;
        selfReportedToolCalls += reported.callCount;
      }
    }
  }
  // ⚠️ 두 도구 카운터를 **합치지 않아요.** `toolCalls` 는 로그 관측이라 조회 창 앞쪽을 못 보고
  // OTel exporter 가 1 MiB 초과 배치를 버려서(2026-09-06 실측) **하한**이에요.
  // `selfReportedToolCalls` 는 세션 전체가 남지만 agent 의 **주장**이에요. 어느 쪽도 총계가
  // 아니고, 「자기보고 없음」이 0 으로 안 그려지게 하는 건 `activityMetricValue` 예요.
  return {
    turnCount: turns.length,
    modelInvokes,
    toolCalls,
    selfReportedToolCalls,
    selfReportedAvailable,
    memoryHits,
    errors,
  };
}
