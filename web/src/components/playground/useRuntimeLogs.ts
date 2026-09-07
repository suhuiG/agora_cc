"use client";

/**
 * 런타임 로그 폴링을 한 곳으로 모아요.
 *
 * 활동 트리가 관측한 runtime 로그를 한 요청/상태 흐름에서 소비하도록 폴링을 hook으로
 * 분리해요. 원문 로그 UI는 IH-115에서 제거됐어요.
 *
 * 커서(`since_ms`) 규약은 그대로예요 — agent 를 바꾸거나 지우면 현재 시각으로 옮겨서 그
 * 전에 찍힌 줄은 의도적으로 보여주지 않아요.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, getAgentRuntimeLogs } from "@/lib/api";
import { createSeenLineFilter } from "@/lib/playground/logDedup";

export const POLL_MS = 4000;

/** 화면에 유지하는 최대 줄 수. 오래 열어두면 DOM 이 무거워져요. */
const MAX_LINES = 400;


export type RuntimeLogState = {
  lines: string[];
  status: string;
  error: string;
  permanentlyStopped: boolean;
  clear: () => void;
};

type Buffer = { recordId: string; lines: string[]; status: string; error: string };

const emptyBuffer = (recordId: string): Buffer => ({
  recordId,
  lines: [],
  status: "",
  error: "",
});

export function useRuntimeLogs({
  recordId,
  enabled,
  onLines,
}: {
  recordId: string;
  enabled: boolean;
  /** 새로 도착한 줄만 넘겨줘요(활동 트리가 진행 중인 턴에 붙여요). */
  onLines?: (lines: string[]) => void;
}): RuntimeLogState {
  const [buffer, setBuffer] = useState<Buffer>(() => emptyBuffer(recordId));
  const sinceRef = useRef<number | undefined>(undefined);
  const onLinesRef = useRef(onLines);
  // 요청을 **직렬화**해요. 없으면 4초보다 오래 걸리는 조회 A·B 가 같은 커서로 겹치고,
  // B 가 먼저 끝나면 뒤늦은 A 가 커서를 과거로 되감아 같은 줄을 또 붙여요(codex 리뷰).
  const inFlightRef = useRef(false);
  // 화면이 지금 보고 있는 agent. 뒤늦은 응답이 자기 것인지 **렌더 사이클과 무관하게**
  // 판정하는 데 써요.
  const currentRecordRef = useRef(recordId);
  // 이미 트리에 붙인 줄. 서버 커서가 도착 여유만큼 뒤에 머물러서 최근 구간이 다시 오는데,
  // 그걸 그대로 붙이면 같은 도구 호출·memory 노드가 여러 번 그려져요 (IH-187).
  // 렌더 사이클과 무관해야 해요 — `onLines` 로 나가는 판정이라 state 로 두면 한 프레임 늦어요.
  // 판정 규칙 자체는 `lib/playground/logDedup.ts` 에 있어요(테스트가 거기 붙어요).
  const seenRef = useRef(createSeenLineFilter());
  // 되감김 방지 — 커서는 단조 증가만 허용해요.
  const cursorGuard = (next: number | undefined) => {
    const current = sinceRef.current;
    if (next === undefined) return;
    if (current === undefined || next > current) sinceRef.current = next;
  };
  // 권한·대상 문제처럼 **영구 실패**면 폴링을 멈춰요. 안 그러면 4초마다 403 을 계속 쳐요.
  const [permanentError, setPermanentError] = useState("");

  // agent 가 바뀌면 이전 agent 의 줄이 섞이지 않게 렌더 중에 버퍼를 맞춰요
  // (React 의 "prop 이 바뀔 때 state 조정" 패턴 — effect 로 미루면 한 프레임 섞여요).
  if (buffer.recordId !== recordId) {
    setBuffer(emptyBuffer(recordId));
    setPermanentError("");
  }

  // 콜백 신원이 바뀌어도 폴링 타이머를 다시 만들지 않게 ref 로 들고 있어요.
  useEffect(() => {
    onLinesRef.current = onLines;
  }, [onLines]);

  useEffect(() => {
    // 커서도 함께 옮겨요 — 안 그러면 새 agent 조회가 이전 구간을 다시 읽어요.
    sinceRef.current = recordId ? Date.now() : undefined;
    // 지금 화면이 보고 있는 agent 를 ref 로도 들고 있어요. 뒤늦게 도착한 응답이 자기 것인지
    // **동기적으로** 판정하는 데 써요. 이 effect 는 아래 폴링 effect 보다 먼저 돌아서,
    // agent 가 바뀐 뒤 도착하는 이전 응답은 항상 거부돼요.
    currentRecordRef.current = recordId;
    // 중복 기억도 여기서 비워요 (IH-187). 안 비우면 새 agent 의 같은 문자열
    // (`Tool #1: …` 처럼 agent 마다 반복되는 줄)이 이전 agent 의 기억에 걸려 조용히
    // 버려져요. 렌더 중이 아니라 이 effect 에서 하는 건 React 규칙(렌더 중 ref 쓰기 금지)
    // 때문이고, 이 effect 가 폴링 effect 보다 먼저 돌아서 순서는 안전해요.
    seenRef.current.reset();
  }, [recordId]);

  const poll = useCallback(async () => {
    if (!recordId || inFlightRef.current) return;
    inFlightRef.current = true;
    // 이 요청이 어느 agent 것인지 고정해요 — 응답 처리 전에 prop 이 바뀔 수 있어요.
    const forRecord = recordId;
    try {
      const res = await getAgentRuntimeLogs(forRecord, sinceRef.current);
      // 이 응답이 아직 화면이 보고 있는 agent 것인지 **동기적으로** 판정해요.
      // 처음엔 `setBuffer` 업데이터 안에서 플래그를 세웠는데, 그 업데이터는 다음 렌더에서야
      // 실행돼서 플래그가 항상 false 였고 **트리에 로그가 아예 안 붙었어요**(브라우저 검증에서
      // 잡음). 그래서 렌더 사이클과 무관한 ref 로 판정해요.
      const accepted = currentRecordRef.current === forRecord;
      if (!accepted) return;
      // 서버 커서가 도착 여유만큼 뒤에 머물러 최근 구간이 다시 와요 — 처음 보는 줄만
      // 남겨요(IH-187). 거부한 응답에는 절대 손대지 않아요(기억이 오염되면 그 줄이
      // 진짜로 올 때 버려져요) — 그래서 `accepted` 판정 **뒤** 예요.
      const fresh = seenRef.current.dropSeen(
        res.lines, res.timestamps, res.event_ids);
      setBuffer((prev) =>
        prev.recordId !== forRecord
          ? prev
          : {
              ...prev,
              status: res.log_status,
              error: "",
              // 마지막 MAX_LINES 줄만 유지 — 오래 열어두면 DOM 이 무거워져요.
              lines:
                fresh.length > 0
                  ? [...prev.lines, ...fresh].slice(-MAX_LINES)
                  : prev.lines,
            },
      );
      cursorGuard(res.next_since_ms || undefined);
      // 거부한 응답은 트리에도 넘기면 안 돼요 — 전에는 `onLines` 를 무조건 불러서
      // 뒤늦은 A agent 로그가 B agent 의 턴에 붙었어요(codex 리뷰).
      if (fresh.length > 0) onLinesRef.current?.(fresh);
    } catch (e) {
      const message = e instanceof Error ? e.message : "로그 조회에 실패했어요.";
      const status = e instanceof ApiError ? e.status : 0;
      // 403(비소유자)·404 는 재시도해도 그대로예요. 폴링을 멈추고 이유를 남겨요.
      const permanent = status === 403 || status === 404;
      if (currentRecordRef.current !== forRecord) return;  // 이전 agent 의 실패는 버려요
      setBuffer((prev) =>
        prev.recordId === forRecord ? { ...prev, error: message } : prev,
      );
      if (permanent) setPermanentError(message);
    } finally {
      inFlightRef.current = false;
    }
  }, [recordId]);

  useEffect(() => {
    if (!enabled || !recordId || permanentError) return;
    const initial = setTimeout(() => void poll(), 0);
    const id = setInterval(() => void poll(), POLL_MS);
    return () => {
      clearTimeout(initial);
      clearInterval(id);
    };
  }, [enabled, recordId, poll, permanentError]);

  const clear = useCallback(() => {
    // 커서도 현재 시각으로 옮겨야 실제로 지워져요 — 버퍼만 비우면 다음 폴링이 같은 구간을
    // 다시 가져와 방금 지운 줄이 되돌아와요.
    sinceRef.current = Date.now();
    setPermanentError("");
    // ⚠️ 중복 기억은 **비우지 않아요** (IH-187). 커서를 지금으로 옮겨도 서버가 도착 여유
    // 구간(15초)을 다시 주기 때문에, 여기서 기억을 비우면 방금 지운 줄이 되돌아와요.
    setBuffer((prev) => ({ ...prev, lines: [], status: "", error: "" }));
  }, []);

  return {
    lines: buffer.lines,
    status: buffer.status,
    error: buffer.error,
    permanentlyStopped: Boolean(permanentError),
    clear,
  };
}
