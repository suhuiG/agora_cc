// web/src/components/playground/PlaygroundChat.tsx
"use client";

/**
 * Playground — 왼쪽 대화, 오른쪽 실행 구성 + 접이식 활동 트리.
 *
 * 시안: `docs/design/playground-activity-tree-mockup.html`.
 *
 * 오른쪽 패널이 답하는 질문은 네 개예요.
 * ① 어떤 **모델**로 도는지 ② 어떤 **도구**(MCP operation·내장 도구)를 쓰는지
 * ③ **기억**을 쓰는지와 장기 메모리 전략 ④ 이 턴에 무슨 일이 있었는지(활동 트리).
 *
 * 값마다 출처를 구분해요 — 선언(배포 원장)인지 실체(runtime 관측)인지. 관측하지 못한 건
 * 만들어 채우지 않고 미관측으로 표시해요(ADR-0037 §4).
 *
 * 가운데 손잡이를 끌면 좌우 폭이 바뀌고, 그 폭은 새로고침 후에도 유지돼요.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  ApiError,
  getAgentRunConfig,
  invokeAgent,
  type DeployedAgent,
} from "@/lib/api";
import {
  type ActivityTurn,
  activitySummary,
  buildActivityTree,
  gateFromStatus,
} from "@/lib/playground/activityTree";
import {
  explainInvokeFailure,
  INVOKE_FAILURE_RAW_LABEL,
  type InvokeFailureExplanation,
  invokeFailureShowsRawSeparately,
  invokeFailureText,
} from "@/lib/playground/invokeErrorMessage";
import {
  type AgentRunConfig,
  observationRequestAction,
  observationRetryAfter,
  requestGenerationMatches,
  type RequestGeneration,
} from "@/lib/playground/runConfig";
import { ROWS_SPLIT } from "@/lib/playground/splitPane";
import { cn } from "@/lib/ui";
import { ActivityTree } from "./ActivityTree";
import { AgentPicker } from "./AgentPicker";
import { RunConfigPanel } from "./RunConfigPanel";
import { SplitPane } from "./SplitPane";
import { useRuntimeLogs } from "./useRuntimeLogs";

// 채팅 한 줄. agent 메시지는 준비중(pending)→타이핑(streaming) 단계를 거쳐요.
type ChatMsg = {
  role: "user" | "agent";
  text: string;        // 화면에 보이는 텍스트(타이핑 중엔 부분 문자열)
  pending?: boolean;   // 응답 대기 중 — 점 인디케이터 표시
  /**
   * 호출이 실패한 경우의 사람 말 설명 + 원문. 있으면 `text` 대신 이걸 그려요.
   *
   * 원문을 «지우지» 않아요 — 접힌 블록으로 항상 도달할 수 있어요.
   */
  failure?: InvokeFailureExplanation;
};

// 응답 글자를 흘리는 속도(ms/글자). 빠르게 채워 긴 답변도 답답하지 않게.
const TYPING_MS = 12;
const OBSERVATION_CACHE_MS = 60_000;
// 실패 config를 사실로 재사용하지 않고 timestamp만 남겨요. runtime probe는 세션을 하나
// 소비하므로 반복 선택은 60초간 억제하되, 일시 장애 복구를 오래 막지 않고 수동 재조회는 열어요.
const OBSERVATION_RETRY_COOLDOWN_MS = 60_000;

// AgentCore runtimeSessionId — 최소 33자 제약이라 UUID 2개를 이어 붙여요.
function newSessionId() {
  return "agora-" + crypto.randomUUID() + crypto.randomUUID();
}

// 배포 완료 agent를 카탈로그에서 골라 대화로 테스트해요. ARN은 서버가 자동 조회해요.
export function PlaygroundChat() {
  const [recordId, setRecordId] = useState("");
  const [selectedAgent, setSelectedAgent] = useState<DeployedAgent | null>(null);
  // 대화별 세션 ID — 최소 33자. 같은 대화 내내 유지해 컨텍스트·microVM stickiness 확보.
  const [sessionId, setSessionId] = useState(newSessionId);
  const [chat, setChat] = useState<ChatMsg[]>([]);
  const [turns, setTurns] = useState<ActivityTurn[]>([]);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const [watching, setWatching] = useState(true);
  const scrollRef = useRef<HTMLDivElement>(null);
  const typingTimer = useRef<number | null>(null);

  const ready = recordId.length > 0;

  // ── 실행 구성(선언) ─────────────────────────────────────────────────
  const [config, setConfig] = useState<AgentRunConfig | null>(null);
  const [configError, setConfigError] = useState("");
  const [configLoading, setConfigLoading] = useState(false);
  const [probing, setProbing] = useState(false);
  const [observedAt, setObservedAt] = useState<number | null>(null);
  const [probeRetryAfter, setProbeRetryAfter] = useState<number | null>(null);
  const observationCache = useRef(new Map<string, {
    config: AgentRunConfig;
    observedAt: number;
  }>());
  const observationRetryAfterByAgent = useRef(new Map<string, number>());

  // 어떤 agent 의 구성을 기다리는지 추적해요. 없으면 느린 A 응답이 이미 B 를 고른 화면을
  // 덮어써요(codex 리뷰). 응답을 받을 때 이 값과 비교해 늦은 응답을 버려요.
  const configGenerationRef = useRef(0);
  const configForRef = useRef<RequestGeneration>({ recordId: "", generation: 0 });

  const loadConfig = useCallback(async (
    id: string,
    probe: boolean,
    force = false,
    request = configForRef.current,
  ): Promise<boolean> => {
    if (!id) return false;
    const isCurrent = () => requestGenerationMatches(configForRef.current, request);
    if (!isCurrent()) return false;
    const cached = observationCache.current.get(id);
    const retryAfter = observationRetryAfterByAgent.current.get(id) ?? null;
    const action = observationRequestAction({
      now: Date.now(),
      successfulObservedAt: cached?.observedAt ?? null,
      retryAfter,
      successCacheMs: OBSERVATION_CACHE_MS,
      force,
    });
    if (probe && action === "reuse_success" && cached) {
      if (!isCurrent()) return false;
      setConfig(cached.config);
      setObservedAt(cached.observedAt);
      setProbeRetryAfter(null);
      setConfigError("");
      setProbing(false);
      setConfigLoading(false);
      return true;
    }
    if (probe && action === "cooldown") {
      if (!isCurrent()) return false;
      setObservedAt(null);
      setProbeRetryAfter(retryAfter);
      setConfigError("");
      setProbing(false);
      setConfigLoading(false);
      return true;
    }
    if (probe) setProbeRetryAfter(null);
    if (probe) setProbing(true);
    else setConfigLoading(true);
    try {
      const next = await getAgentRunConfig(id, probe);
      const timestamp = Date.now();
      const nextRetryAfter = probe
        ? observationRetryAfter(
          next.observed.status,
          timestamp,
          OBSERVATION_RETRY_COOLDOWN_MS,
        )
        : null;
      if (!isCurrent()) return false;
      if (probe && nextRetryAfter === null) {
        observationCache.current.set(id, { config: next, observedAt: timestamp });
        observationRetryAfterByAgent.current.delete(id);
      } else if (probe && nextRetryAfter !== null) {
        observationCache.current.delete(id);
        observationRetryAfterByAgent.current.set(id, nextRetryAfter);
      }
      setConfig(next);
      setConfigError("");
      if (probe && next.observed.status === "ok") {
        setObservedAt(timestamp);
        setProbeRetryAfter(null);
      } else if (probe && nextRetryAfter !== null) {
        setObservedAt(null);
        setProbeRetryAfter(nextRetryAfter);
      } else {
        setObservedAt(null);
      }
      return true;
    } catch (e) {
      if (!isCurrent()) return false;
      const retryAt = Date.now() + OBSERVATION_RETRY_COOLDOWN_MS;
      if (probe) {
        observationCache.current.delete(id);
        observationRetryAfterByAgent.current.set(id, retryAt);
      }
      if (probe) setProbeRetryAfter(retryAt);
      setConfigError(e instanceof Error ? e.message : "실행 구성을 읽지 못했어요.");
      return false;
    } finally {
      if (isCurrent()) {
        setProbing(false);
        setConfigLoading(false);
      }
    }
  }, []);

  // ── 런타임 로그 → 활동 트리 ────────────────────────────────────────
  // 새로 도착한 줄은 "지금 열려 있는 마지막 턴"에 붙여요. 로그에 턴 경계 표시가 없어서
  // 도착 순서로 배치하는 근사예요 — 정확한 span 계층은 OTEL span 을 읽어야 해요.
  const appendLines = useCallback((lines: string[]) => {
    setTurns((prev) => {
      if (prev.length === 0) return prev;
      const next = [...prev];
      const last = next[next.length - 1];
      next[next.length - 1] = { ...last, lines: [...last.lines, ...lines] };
      return next;
    });
  }, []);

  const runtimeLogs = useRuntimeLogs({
    recordId,
    enabled: watching && ready,
    onLines: appendLines,
  });

  const declaredToolCount = config?.declared.tools.length ?? 0;
  const nodes = useMemo(
    () => buildActivityTree(turns, { declaredToolCount }),
    [turns, declaredToolCount],
  );
  const summary = useMemo(() => activitySummary(turns), [turns]);

  // agent를 바꾸면 대화를 새로 시작해요 — 이력·입력·세션 ID를 전부 리셋.
  // 세션 ID까지 새로 발급하는 게 핵심이에요. 안 그러면 새 agent가 이전 agent의
  // AgentCore 세션(컨텍스트·microVM)을 물려받아, 화면만 비워도 대화가 이어져요.
  function selectAgent(agent: DeployedAgent) {
    const next = agent.record_id;
    if (next === recordId) return;
    if (typingTimer.current) {              // 타이핑 중이던 이전 응답을 멈춰요.
      clearTimeout(typingTimer.current);
      typingTimer.current = null;
    }
    setRecordId(next);
    setSelectedAgent(agent);
    setChat([]);
    setTurns([]);
    setInput("");
    setSessionId(newSessionId());
    setConfig(null);
    setConfigError("");
    setObservedAt(null);
    setProbeRetryAfter(null);
    const request = {
      recordId: next,
      generation: ++configGenerationRef.current,
    };
    configForRef.current = request;
    // 선언을 먼저 그린 뒤 selfcheck를 이어서 실행해, 느린 probe가 원장값까지 가리지 않아요.
    if (next) {
      const cached = observationCache.current.get(next);
      const action = observationRequestAction({
        now: Date.now(),
        successfulObservedAt: cached?.observedAt ?? null,
        retryAfter: observationRetryAfterByAgent.current.get(next) ?? null,
        successCacheMs: OBSERVATION_CACHE_MS,
      });
      if (action === "reuse_success") {
        void loadConfig(next, true, false, request);
      } else {
        void loadConfig(next, false, false, request).then((loaded) => {
          if (loaded && requestGenerationMatches(configForRef.current, request)) {
            void loadConfig(next, true, false, request);
          }
        });
      }
    }
  }

  // 새 메시지·타이핑 진행 때마다 맨 아래로 스크롤해요.
  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, [chat]);

  // 마지막 agent 메시지에 도착한 full 응답을 글자 단위로 흘려보내요(가짜 스트리밍).
  function streamReply(fullText: string) {
    let i = 0;
    const tick = () => {
      i += 1;
      setChat((c) => {
        const next = [...c];
        const last = next[next.length - 1];
        if (last && last.role === "agent") {
          next[next.length - 1] = { ...last, text: fullText.slice(0, i), pending: false };
        }
        return next;
      });
      if (i < fullText.length) {
        typingTimer.current = window.setTimeout(tick, TYPING_MS);
      } else {
        typingTimer.current = null;
      }
    };
    tick();
  }

  useEffect(() => () => { if (typingTimer.current) clearTimeout(typingTimer.current); }, []);

  function finishTurn(patch: Partial<ActivityTurn>) {
    setTurns((prev) => {
      if (prev.length === 0) return prev;
      const next = [...prev];
      next[next.length - 1] = { ...next[next.length - 1], ...patch };
      return next;
    });
  }

  async function send() {
    if (!ready || !input.trim() || sending) return;
    const msg = input.trim();
    // user 메시지 + agent 준비중(pending) 버블을 함께 넣어요.
    setChat((c) => [...c, { role: "user", text: msg }, { role: "agent", text: "", pending: true }]);
    setTurns((prev) => [
      ...prev,
      {
        index: prev.length + 1,
        prompt: msg,
        roundTripMs: null,
        state: "running",
        gate: "unknown",
        lines: [],
      },
    ]);
    setInput("");
    setSending(true);
    // 포털 왕복만 측정해요 — 모델 지연이 아니라 브라우저→BFF→Runtime 왕복이에요.
    const startedAt = performance.now();
    try {
      const res = await invokeAgent(recordId, msg, sessionId);
      finishTurn({
        roundTripMs: performance.now() - startedAt,
        state: "done",
        invocationId: res.invocation_id,
        // 200 이면 agent-invoke 게이트를 통과한 거예요(게이트가 Runtime 호출 앞에 있어요).
        gate: gateFromStatus(null),
        // agent 자기보고 사용량 — 트리가 이걸로 「이 턴이 부른 도구」를 그려요. 로그 관측과
        // 별개 출처라 합치지 않고 나란히 보여줘요.
        usage: res.usage,
      });
      streamReply(res.result);
    } catch (e) {
      const failure = explainInvokeFailure(e);
      const errText = invokeFailureText(failure);
      setChat((c) => {
        const next = [...c];
        next[next.length - 1] = {
          role: "agent",
          text: errText,
          pending: false,
          failure,
        };
        return next;
      });
      finishTurn({
        roundTripMs: performance.now() - startedAt,
        state: "error",
        // 트리 상세도 원문에 도달해야 해요 — `invokeFailureText` 가 원문을 포함해요.
        errorText: errText,
        gate: gateFromStatus(e instanceof ApiError ? e.status : 0),
      });
    } finally {
      setSending(false);
    }
  }

  const selectedName = selectedAgent?.name ?? "";

  const chatPane = (
    <div className="flex h-full min-h-0 flex-col overflow-hidden rounded-xl border border-border bg-card lg:mr-2">
      <div ref={scrollRef} className="min-h-0 flex-1 space-y-3 overflow-y-auto p-4">
        {chat.length === 0 ? (
          <p className="py-16 text-center text-sm text-muted-foreground">
            {ready
              // 어느 agent와의 새 대화인지 알려줘요 — agent를 바꿔 대화가 비워진 직후에
              // "왜 비었는지"가 바로 읽혀요.
              ? `${selectedName || "agent"}와의 새 대화예요. 메시지를 입력하면 시작돼요.`
              : "먼저 agent를 선택해 주세요."}
          </p>
        ) : (
          chat.map((m, i) => (
            <div key={i} className={m.role === "user" ? "text-right" : "text-left"}>
              {/* 실패 버블은 목록·접힌 블록을 담아서 `div` 예요(`span` 안에 두면 중첩이 깨져요). */}
              <div className={
                "inline-block max-w-[85%] whitespace-pre-wrap rounded-2xl px-4 py-2 text-sm " +
                (m.role === "user" ? "bg-blue-600 text-white" : "bg-accent text-foreground")
              }>
                {m.pending
                  ? <TypingDots />
                  : m.failure
                    ? <InvokeFailureBubble failure={m.failure} />
                    : m.text}
              </div>
            </div>
          ))
        )}
      </div>

      <div className="flex gap-2 border-t border-border bg-slate-50/60 p-3">
        <Input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter" && !e.nativeEvent.isComposing) send(); }}
          placeholder={ready ? "메시지 입력…" : "agent를 먼저 선택해 주세요"}
          disabled={!ready || sending}
        />
        <Button variant="primary" onClick={send} disabled={!ready || sending || !input.trim()}
          className="shrink-0 whitespace-nowrap px-5">
          {sending ? "…" : "전송"}
        </Button>
      </div>
    </div>
  );

  // 실행 구성은 자기 카드에서 스크롤해요 — 트리 카드와 높이를 나눠 쓰고, 가로 손잡이로
  // 그 비율을 바꿔요. 전에는 한 카드 안에 쌓여 있어서 구성이 길면 트리가 눌렸어요.
  const configCard = (
    <div className="flex h-full min-h-0 flex-col overflow-hidden rounded-xl border border-border bg-card lg:ml-2">
      <div className="shrink-0 border-b border-border px-4 py-2 text-sm font-semibold text-foreground">
        실행 구성 · 모델 · 도구 · 기억
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto">
        <RunConfigPanel
          config={config}
          loading={configLoading}
          error={configError}
          probing={probing}
          observedAt={observedAt}
          cacheMs={OBSERVATION_CACHE_MS}
          retryAfter={probeRetryAfter}
          retryCooldownMs={OBSERVATION_RETRY_COOLDOWN_MS}
          onProbe={() => void loadConfig(recordId, true, true)}
        />
      </div>
    </div>
  );

  const treeCard = (
    <div className="flex h-full min-h-0 flex-col overflow-hidden rounded-xl border border-border bg-card lg:ml-2">
      <div className="min-h-0 flex-1 overflow-hidden">
        <ActivityTree
          nodes={nodes}
          summary={summary}
          live={watching && ready && (sending || turns.length > 0)}
          runtimeLogs={{
            ready,
            status: runtimeLogs.status,
            error: runtimeLogs.error,
            permanentlyStopped: runtimeLogs.permanentlyStopped,
            enabled: watching && ready,
          }}
        />
      </div>

      <div className="flex shrink-0 items-center gap-2 border-t border-border bg-slate-50/60 px-4 py-1.5">
        <button
          type="button"
          onClick={() => setWatching((v) => !v)}
          aria-pressed={watching}
          className={cn(
            "rounded px-2 py-1 text-[11px] hover:bg-accent",
            watching ? "text-muted-foreground" : "text-blue-700",
          )}
        >
          {watching ? "관측 일시정지" : "관측 재개"}
        </button>
        <span className="text-[11px] text-slate-400">
          런타임 로그 폴링 · CloudWatch 조회가 일어나요
        </span>
      </div>

    </div>
  );

  const activityPane = (
    <SplitPane
      spec={ROWS_SPLIT}
      first={configCard}
      second={treeCard}
      firstLabel="실행 구성"
      secondLabel="활동 트리"
      className="h-full gap-0"
    />
  );

  return (
    <div className="mx-auto max-w-[1400px] space-y-4">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Playground</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          배포 완료된 agent를 골라 대화로 테스트해요. 오른쪽에서 어떤 모델·도구·기억으로
          도는지와, 이 턴에 관측된 활동을 트리로 볼 수 있어요.
        </p>
      </div>

      <div>
        <label className="mb-1.5 block text-xs font-medium text-muted-foreground">agent 선택</label>
        <AgentPicker
          selected={selectedAgent}
          disabled={sending}
          onSelect={selectAgent}
        />
      </div>

      {/* 전체 높이를 여기서 한 번만 정해요 — 안쪽 카드는 전부 h-full 로 이걸 나눠 써요. */}
      <SplitPane
        first={chatPane}
        second={activityPane}
        firstLabel="대화"
        secondLabel="오른쪽 패널"
        className="h-[min(78vh,820px)] min-h-[560px]"
      />
    </div>
  );
}

/**
 * 호출 실패 버블 — 사람 말 한 줄 + 처방 목록 + **접힌 원문**.
 *
 * ⚠️ 원문 블록을 지우지 마세요. 예외 문구가 진단의 유일한 단서인 경우가 있고, 이 저장소는
 * 「관측한 것을 숨기지 않는다」가 규율이에요. 모르는 예외는 `headline` 이 곧 원문이라
 * (`invokeFailureShowsRawSeparately` 가 거짓) 두 번 그리지 않아요.
 */
function InvokeFailureBubble({ failure }: { failure: InvokeFailureExplanation }) {
  return (
    <div className="text-left">
      <p className="font-medium">{failure.headline}</p>
      {failure.steps.length > 0 && (
        <ol className="mt-1.5 list-decimal space-y-1 pl-4 text-[13px] leading-relaxed">
          {failure.steps.map((step, i) => (
            <li key={i}>{step}</li>
          ))}
        </ol>
      )}
      {invokeFailureShowsRawSeparately(failure) && (
        <details className="mt-2">
          <summary className="cursor-pointer text-[11px] text-muted-foreground">
            {INVOKE_FAILURE_RAW_LABEL}
          </summary>
          <pre className="mt-1 max-h-40 overflow-auto whitespace-pre-wrap break-all rounded bg-slate-100 p-2 text-[11px] leading-relaxed text-slate-700">
            {failure.raw}
          </pre>
        </details>
      )}
    </div>
  );
}

// 응답 준비중 표시 — 점 3개가 순차로 튀어올라 "생각 중" 느낌을 줘요.
function TypingDots() {
  return (
    <span className="inline-flex items-center gap-1 py-1" aria-label="응답 준비중">
      <span className="typing-dot" />
      <span className="typing-dot" style={{ animationDelay: "0.15s" }} />
      <span className="typing-dot" style={{ animationDelay: "0.3s" }} />
      <style>{`
        .typing-dot {
          width: 6px;
          height: 6px;
          border-radius: 9999px;
          background-color: currentColor;
          opacity: 0.4;
          animation: agora-typing 1s ease-in-out infinite;
        }
        @keyframes agora-typing {
          0%, 60%, 100% { transform: translateY(0); opacity: 0.4; }
          30% { transform: translateY(-4px); opacity: 1; }
        }
      `}</style>
    </span>
  );
}
