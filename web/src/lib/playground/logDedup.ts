/**
 * 런타임 로그 재전송 걸르기 (IH-187).
 *
 * 서버 커서가 도착 여유(`AgentRuntimeLogReader.INGESTION_GRACE_MS`, 15초)만큼 뒤에 머물러요 —
 * 같은 컨테이너의 줄이 도착 순서가 달라서, 커서를 마지막 이벤트 뒤로 올리면 3~5초 늦게 도착하는
 * logging 줄(`model 호출`)이 영구히 유실되기 때문이에요. 그 대가로 **최근 구간이 매 폴링마다
 * 다시 와요.** 여기서 걸러요.
 *
 * ⭐ 판정 키는 **CloudWatch `eventId`** 예요 — 이벤트의 진짜 신원이에요. 줄 문자열만 쓰면
 * «같은 문자열의 새 이벤트» 까지 버려지고(같은 도구를 두 턴에서 부르면 `Tool #1: x` 가 글자까지
 * 같아요), `시각|줄` 로도 **같은 밀리초의 동일 문자열 두 건**은 못 갈려요(codex 리뷰 2026-09-07,
 * 두 라운드). 그 결함은 트리와 카운트를 조용히 작게 만들어요.
 *
 * 되떨어지는 순서는 `eventId` → `시각|줄` → `줄` 이에요. 앞의 것이 없는 응답(옛 서버)에서만
 * 뒤로 내려가고, 그 경우에만 위의 손실이 남아요.
 *
 * 이 모듈을 훅에서 갈라 둔 이유: 걸르기 규칙은 «판정» 이라 테스트가 붙어야 하는데, 훅 안에
 * 두면 React 없이 부를 수 없어요.
 */

/**
 * 기억해 두는 키 개수. 4초 폴링 × 15초 창이면 한 줄이 최대 4번 오니, 한 턴 분량(수백 줄)의
 * 몇 배를 기억하면 충분해요. 무한히 늘리지 않으려고 오래된 쪽부터 잊어요.
 */
export const DEDUP_WINDOW = 2000;

/**
 * 중복 판정 키. `eventId` → `시각|줄` → `줄` 순으로 되떨어져요.
 *
 * `eventId` 는 이벤트당 하나뿐인 값이라 여기서 멈추면 오판이 없어요. 뒤의 둘은 옛 서버용
 * 차선이고, 그 구간에서만 「같은 문자열의 새 이벤트」가 유실될 수 있어요.
 */
export function dedupKey(
  line: string,
  ts: number | null | undefined,
  eventId?: string | null,
): string {
  if (typeof eventId === "string" && eventId) return `id:${eventId}`;
  return typeof ts === "number" ? `${ts}|${line}` : line;
}

export type SeenLineFilter = {
  /** 처음 보는 줄만 남겨요. 순서는 그대로 지켜요(트리가 도착 순으로 노드를 쌓아요). */
  dropSeen: (
    lines: string[],
    timestamps?: (number | null)[],
    eventIds?: (string | null)[],
  ) => string[];
  /** 기억을 비워요. **agent 를 바꿀 때만** 불러요 — 이유는 `useRuntimeLogs` 주석에 있어요. */
  reset: () => void;
  /** 지금 기억하는 키 개수(테스트·진단용). */
  size: () => number;
};

export function createSeenLineFilter(windowSize: number = DEDUP_WINDOW): SeenLineFilter {
  let seen = new Set<string>();
  let order: string[] = [];

  return {
    dropSeen(lines, timestamps, eventIds) {
      const fresh: string[] = [];
      lines.forEach((line, i) => {
        const key = dedupKey(line, timestamps?.[i], eventIds?.[i]);
        if (seen.has(key)) return;
        seen.add(key);
        order.push(key);
        fresh.push(line);
      });
      while (order.length > windowSize) {
        const evicted = order.shift();
        if (evicted !== undefined) seen.delete(evicted);
      }
      return fresh;
    },
    reset() {
      seen = new Set();
      order = [];
    },
    size: () => seen.size,
  };
}
