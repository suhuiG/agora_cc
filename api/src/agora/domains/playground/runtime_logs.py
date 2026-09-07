"""배포 agent의 CloudWatch 런타임 로그 조회 — Playground 로그 패널 백엔드.

왜 필요한가: Agora엔 배포된 agent의 로그를 보는 경로가 없어서, 도구가 안 붙어도
사용자가 원인을 알 수 없었어요(2026-07-27 실사용에서 드러남 — 진단이 CLI 수동
작업이었어요). Playground에서 바로 `Tool #N` 호출·토큰 경고·에러를 보게 해요.

페이지네이션 루프는 거버넌스 스캔 로그 리더(governance/scan_logs.py)의
fetch_group을 재사용해요 — 첫 페이지만 읽으면 empty로 오판하는 함정이 같아요.
"""
from __future__ import annotations

import time

from ..governance.scan_logs import ScanLogReader


def runtime_id_from_arn(runtime_arn: str) -> str:
    """runtimeArn → runtime_id. 관례는 catalog/router.py의 재배포 경로와 동일해요."""
    return (runtime_arn or "").rsplit("/", 1)[-1] if runtime_arn else ""


def log_group_for_runtime(runtime_id: str) -> str:
    """AgentCore Runtime 로그그룹 규약(실측 2026-07-27)."""
    return f"/aws/bedrock-agentcore/runtimes/{runtime_id}-DEFAULT"


class AgentRuntimeLogReader:
    """배포된 agent 컨테이너 로그를 읽어요.

    리전은 deploy_region이어야 해요 — 스캔 로그 리더는 scan_region을 쓰는데
    AgentCore Runtime은 deploy_region에 배포돼요(기본값이 같아 우연히 일치할 뿐).
    """

    # 첫 조회 되돌아보기 구간(30분). 전체를 훑으면 오래된 이벤트 때문에 느려요.
    LOOKBACK_MS = 30 * 60 * 1000

    # AgentCore 헬스체크(GET /ping)는 초 단위로 찍혀서 화면을 도배해요 — 정작 중요한
    # Tool 호출·경고가 밀려나서 기본으로 걸러요(include_health=True로 볼 수 있어요).
    _NOISE = ('"GET /ping HTTP', "GET /ping HTTP/1.1")

    # CloudWatch 도착 여유 (IH-187). 커서를 이만큼 뒤에 붙잡아 둬요.
    #
    # 왜 필요한가: 한 컨테이너의 줄이 **같은 순서로 도착하지 않아요.** 실측 2026-09-07
    # (`cs_assistant-0k6tSH9WaA-DEFAULT`, `ingestionTime - timestamp`) — `Tool #N` 은
    # `print()` 라 1.1초, logging 줄(`strands.event_loop.streaming`)은 3.1초·4.7초예요.
    # 커서를 `last_ts + 1` 로 올리면 «나중에 찍혔지만 먼저 도착한» TOOL 줄이 커서를
    # 끌어올려서, 그보다 앞선 시각의 MODEL 줄이 다음 조회 창 밖으로 밀려 **영구히**
    # 안 보여요. 화면 `model 호출` 이 늘 `0` 이던 이유가 이거예요.
    #
    # 15초는 실측 최대 지연(4.7초)의 3배예요. 대가는 **재전송** — 최근 15초 구간을 매
    # 폴링마다 다시 읽어요. 그래서 소비자가 중복을 걸러야 해요
    # (`web/src/components/playground/useRuntimeLogs.ts` 의 본 줄 집합).
    INGESTION_GRACE_MS = 15 * 1000

    def __init__(self, *, region: str, client=None, now_ms=None) -> None:
        self._region = region
        self._client = client
        self._now_ms = now_ms or (lambda: int(time.time() * 1000))

    def fetch(self, runtime_id: str, *, limit: int = 200,
              since_ms: int | None = None, include_health: bool = False) -> dict:
        """로그 라인을 읽어요.

        반환: {"log_status": ok|empty|unavailable, "lines": [...],
               "timestamps": [...], "event_ids": [...], "next_since_ms": int} —
        next_since_ms를 다음 폴링에 넘기면 새로 생긴 줄만 받아요(증분 tail).

        `timestamps`·`event_ids`는 `lines`와 같은 길이·순서예요(IH-187). 소비자가 중복을
        걸러야 하는데(`_next_cursor` 참조) 줄 문자열만으로는 «같은 문자열의 새 이벤트»와
        «재전송»을 못 가려요. 판정 키는 CloudWatch `eventId`(이벤트의 진짜 신원)이고,
        시각은 그게 없을 때의 차선이에요 — 시각+본문은 같은 밀리초의 동일 문자열 두 건을
        못 가려요(codex 리뷰 2026-09-07).

        `limit`은 **읽기 목표치**예요. 필터 뒤 남은 줄은 잘라내지 «않아요» — 잘라내면
        커서가 안 보낸 줄을 지나쳐 그 줄이 영구히 유실돼요(codex 리뷰 2026-09-07,
        IH-187). 읽는 양은 `raw_limit`(최대 1,000 이벤트)에서 이미 묶여 있어요.

        include_health=False(기본)면 헬스체크 핑을 걸러요. 안 걸르면 초 단위 핑이
        화면을 덮어 Tool 호출·경고가 안 보여요(실측: 40줄 중 대부분이 /ping).
        """
        if not runtime_id:
            return {"log_status": "unavailable", "lines": [], "timestamps": [],
                    "event_ids": [], "next_since_ms": since_ms or 0}
        start = since_ms if since_ms is not None else self._now_ms() - self.LOOKBACK_MS
        # 스캔 리더와 페이지네이션 규약을 공유해요(로그그룹만 다름).
        # stage는 스캔 도구 로그그룹명 파생에만 쓰여서 여기선 의미가 없어요(fetch_group은 안 씀).
        reader = ScanLogReader(region=self._region, stage="", client=self._client)
        # 노이즈를 거를 땐 더 넉넉히 읽어요 — 핑이 대부분이면 걸러낸 뒤 남는 게 없어요.
        raw_limit = limit if include_health else min(limit * 5, 1000)
        out = reader.fetch_group(
            log_group_for_runtime(runtime_id), start_time=start, limit=raw_limit)
        # 줄·시각·이벤트 id 를 **한 짝으로** 걸러요 — 인덱스가 어긋나면 중복 판정 키가
        # 다른 줄의 신원을 물어요.
        rows = self._aligned_rows(out)
        if not include_health:
            rows = [row for row in rows
                    if not any(n in row[0] for n in self._NOISE)]
        lines = [ln for ln, _, _ in rows]
        timestamps = [ts for _, ts, _ in rows]
        event_ids = [eid for _, _, eid in rows]
        # ⭐ 커서는 `out["last_ts"]` 를 써요 — 이건 `fetch_group` 이 **돌려준 줄들** 의
        #    최댓값이라(IH-187 로 그렇게 바꿨어요) 안 보낸 줄을 지나치지 않아요. 걸러낸
        #    핑도 커서를 전진시켜야 해요 — 핑만 있는 구간에서 커서가 멈추면 같은 구간을
        #    영원히 다시 읽어요. 그래서 필터 **전** 의 최댓값이 맞아요.
        return {
            # 핑만 있고 실질 로그가 없으면 empty로 알려줘요(사용자에겐 빈 화면이니까).
            "log_status": "ok" if lines else ("empty" if out["log_status"] == "ok"
                                              else out["log_status"]),
            "lines": lines,
            "timestamps": timestamps,
            "event_ids": event_ids,
            "next_since_ms": self._next_cursor(start, out.get("last_ts")),
        }

    @staticmethod
    def _aligned_rows(out: dict) -> list[tuple[str, int | None, str | None]]:
        """`lines`·`timestamps`·`event_ids` 를 길이가 맞는 행으로 묶어요.

        길이가 어긋나면 **그 컬럼만 통째로 버려요**(`None` 채움). 짧은 쪽에 맞춰 zip 하면
        뒤쪽 줄이 사라지고, 남는 쪽에 맞추면 남의 시각·신원을 물어요 — 둘 다 조용히
        틀려요.
        """
        lines = out.get("lines") or []
        ts = out.get("timestamps") or []
        ids = out.get("event_ids") or []
        if len(ts) != len(lines):
            ts = [None] * len(lines)
        if len(ids) != len(lines):
            ids = [None] * len(lines)
        return [(lines[i], ts[i], ids[i]) for i in range(len(lines))]

    def _next_cursor(self, start: int, last_ts: object) -> int:
        """다음 조회 시작점. **도착 여유를 넘어서 전진하지 않아요** (IH-187).

        옛 규약은 `last_ts + 1` 이었어요. 같은 컨테이너의 줄이 도착 순서가 달라서
        (`INGESTION_GRACE_MS` 주석의 실측), 그러면 늦게 도착하는 줄이 영구히 유실돼요.

        세 경계를 지켜요.

        1. `now - GRACE` 를 **넘지 않아요** — 아직 도착 중인 구간을 다시 읽어요.
        2. 여유 구간보다 오래된 줄에서는 **정상 전진해요** — 안 그러면 같은 줄을 무한
           재전송해요.
        3. `start` 보다 **뒤로 가지 않아요** — 되감으면 사용자가 지운 줄이 돌아와요.
        """
        advanced = (last_ts + 1) if isinstance(last_ts, int) else start
        safe = self._now_ms() - self.INGESTION_GRACE_MS
        return max(start, min(advanced, safe))
