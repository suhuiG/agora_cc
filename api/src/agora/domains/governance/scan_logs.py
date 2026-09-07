"""게이트 스캔 로그 조회 — tool_id를 CloudWatch 로그그룹으로 매핑하고 scan_id로 필터.

로그그룹 규약(scan-tools-stack.ts):
  Lambda 도구(gitleaks·trivy·llm-judge): /aws/lambda/agora-tool-{tool_id}-{stage}
  Fargate 도구(semgrep): /agora/scan-tool/{tool_id}-{stage}  (Task 0에서 CDK로 고정)

scan_id는 스캐너 실행에 payload/env로 전달돼요. 로그에서 scan_id를 못 찾으면 빈 결과를
안전 반환해요(스캐너가 로그에 안 찍을 수 있음 — modal은 findings·상태로 폴백).
"""
from __future__ import annotations

from datetime import datetime, timezone

LAMBDA_TOOLS = {"gitleaks", "trivy", "llm-judge"}
FARGATE_TOOLS = {"semgrep"}

# 페이지네이션 상한 — Lambda 로그그룹엔 오래된 START/END/REPORT가 대량이라 첫 페이지에
# 매칭이 없을 수 있어요(실측 2026-07-21: 매칭 9건이 7페이지 뒤에 있었음). nextToken을
# 따라가되, 폭주를 막게 상한을 둬요.
_MAX_PAGES = 40


def log_group_for(tool_id: str, stage: str) -> str | None:
    if tool_id in LAMBDA_TOOLS:
        return f"/aws/lambda/agora-tool-{tool_id}-{stage}"
    if tool_id in FARGATE_TOOLS:
        return f"/agora/scan-tool/{tool_id}-{stage}"
    return None


def _start_time_ms(scan_id: str) -> int | None:
    """scan_id 꼬리의 타임스탬프(...-YYYYMMDDHHMMSS)에서 조회 시작 시각(ms)을 뽑아요.

    이 시각 1시간 전부터 조회하면 오래된 START/END/REPORT 이벤트를 안 훑어서
    페이지네이션 부담이 근본적으로 줄어요. 형식이 안 맞으면 None(전체 범위 폴백).
    KST가 아니라 스캐너가 찍는 UTC 벽시계 기준이지만, 1시간 여유로 tz 오차를 흡수해요.
    """
    tail = scan_id.rsplit("-", 1)[-1]
    if len(tail) != 14 or not tail.isdigit():
        return None
    try:
        dt = datetime.strptime(tail, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return int(dt.timestamp() * 1000) - 3_600_000  # 1시간 전부터(tz·클럭 오차 흡수)


class ScanLogReader:
    """CloudWatch Logs에서 특정 스캔(scan_id)의 도구 실행 로그를 읽어요.

    filter_log_events로 scan_id 포함 라인만 조회. 로그그룹 없음·조회 실패는 unavailable로
    흡수(모달을 막지 않음). scan_id를 로그에서 못 찾으면 empty.
    """
    def __init__(self, *, region, stage, client=None) -> None:
        self._region = region
        self._stage = stage
        self._client = client

    def _logs(self):
        if self._client is None:
            import boto3
            self._client = boto3.client("logs", region_name=self._region)
        return self._client

    def fetch(self, tool_id: str, scan_id: str, limit: int = 200) -> dict:
        group = log_group_for(tool_id, self._stage)
        if not group:
            return {"log_status": "unavailable", "lines": []}
        # quoted term = 정확한 부분 문자열 매칭. 실측(2026-07-21 재확정): `?term`은
        # 느슨한 OR 매칭이라 scan_id 무관하게 대량 반환하고, bare term은 하이픈에서
        # 토큰이 쪼개져 안 잡혀요. `"scan_id"`(따옴표)로 감싸야 정확히 그 라인만 걸려요.
        return self.fetch_group(
            group, filter_pattern=f'"{scan_id}"',
            start_time=_start_time_ms(scan_id), limit=limit)

    def fetch_group(self, log_group: str, *, filter_pattern: str = "",
                    start_time: int | None = None, limit: int = 200) -> dict:
        """로그그룹에서 라인을 읽어요. 스캔 도구·agent 런타임이 공용으로 써요.

        반환: {"log_status": ok|empty|unavailable, "lines": [...],
               "timestamps": [...], "event_ids": [...], "last_ts": int|None}

        `timestamps`·`event_ids`는 `lines`와 **같은 길이·같은 순서**예요(IH-187). 없는
        값은 `None`이에요. 세 리스트를 함께 잘라서 인덱스가 어긋나지 않아요.

        `event_ids`는 CloudWatch `filter_log_events` 의 `eventId` 예요 — **이벤트의 진짜
        신원**이라 소비자의 중복 판정 키가 돼요. 시각+본문으로는 같은 밀리초에 찍힌 동일
        문자열 두 건을 못 가려요(codex 리뷰 2026-09-07).

        ⚠️ `last_ts`는 **돌려준 줄들의** 최댓값이에요 — 읽었지만 `limit`에 걸려 잘려나간
        이벤트는 세지 않아요(IH-187). 잘린 이벤트까지 세면 증분 tail 이 «안 보낸 줄» 을
        지나쳐서 그 줄이 영구히 유실돼요. 마지막 페이지는 `limit`을 넘겨 읽힐 수 있으니
        이 구분이 실제로 갈려요.

        ⚠️ 그리고 **같은 밀리초 안에서 자르지 않아요.** 증분 tail 의 다음 시작점은
        `last_ts + 1` 이라, 시각 `T` 인 이벤트 다섯 건의 가운데서 자르면 남은 세 건이
        다음 창(`T+1` 부터)에서 영구히 빠져요. 그래서 잘린 경우 마지막 «완결된» 밀리초
        까지만 돌려줘요.

        예외는 unavailable로 흡수해 UI를 막지 않아요.
        """
        kwargs: dict = {"logGroupName": log_group, "limit": limit}
        if filter_pattern:
            kwargs["filterPattern"] = filter_pattern
        if start_time is not None:
            kwargs["startTime"] = start_time
        lines: list[str] = []
        timestamps: list[int | None] = []
        event_ids: list[str | None] = []
        try:
            token = None
            # 페이지네이션: filter_log_events는 매칭이 없어도 nextToken만 든 빈 페이지를
            # 반환해요(실측 2026-07-21: Lambda 로그그룹은 첫 페이지 0건, 매칭은 7페이지 뒤).
            # 첫 페이지만 보면 작은 그룹만 되고 큰 그룹은 empty로 오판돼요.
            # 원하는 라인 수를 채우거나 토큰이 끝날 때까지(상한) 따라가요.
            for _ in range(_MAX_PAGES):
                if token:
                    kwargs["nextToken"] = token
                resp = self._logs().filter_log_events(**kwargs)
                for e in resp.get("events", []):
                    lines.append(e.get("message", "").rstrip("\n"))
                    ts = e.get("timestamp")
                    timestamps.append(ts if isinstance(ts, int) else None)
                    eid = e.get("eventId")
                    event_ids.append(eid if isinstance(eid, str) and eid else None)
                token = resp.get("nextToken")
                if not token or len(lines) >= limit:
                    break
        except Exception:
            return {"log_status": "unavailable", "lines": [], "timestamps": [],
                    "event_ids": [], "last_ts": None}
        # 더 읽을 게 남았나 — 남았다면 마지막 밀리초 그룹이 «다음 페이지로 이어질» 수
        # 있어요. 이어지는지 여기서는 알 수 없으니 잘라내는 쪽을 골라요(그 이벤트는 커서가
        # 그 시각 아래에 머물러서 다음 폴링에 다시 와요).
        more_available = len(lines) > limit or token is not None
        keep = self._cut_on_a_millisecond_boundary(timestamps, limit, more_available)
        kept_lines, kept_ts = lines[:keep], timestamps[:keep]
        kept_ids = event_ids[:keep]
        # ⭐ 돌려주는 줄들만으로 최댓값을 내요 — 잘려나간 이벤트를 세면 증분 tail 이 그 줄을
        #    지나쳐요(IH-187).
        seen = [ts for ts in kept_ts if isinstance(ts, int)]
        return {"log_status": "ok" if kept_lines else "empty",
                "lines": kept_lines, "timestamps": kept_ts,
                "event_ids": kept_ids,
                "last_ts": max(seen) if seen else None}

    @staticmethod
    def _cut_on_a_millisecond_boundary(timestamps: list[int | None], limit: int,
                                       more_available: bool) -> int:
        """`limit`까지 자르되, **같은 밀리초를 가르지 않는** 개수를 돌려줘요 (IH-187).

        증분 tail 의 다음 시작점은 `last_ts + 1` 이에요. 시각 `T` 인 이벤트 다섯 건의
        가운데서 자르면 남은 세 건은 다음 창(`T+1` 부터)에 안 잡혀 **영구히** 사라져요.

        `more_available` 이 핵심이에요. CloudWatch 는 한 페이지에 정확히 `limit` 건까지
        주므로, 「우리가 상한을 넘겨 읽었나」로는 부족해요 — **다음 페이지에 그 밀리초가
        이어질 수** 있어요. 남은 데이터가 있으면 마지막 밀리초 그룹을 통째로 물러요. 그
        이벤트들은 커서가 그 시각 아래에 머물러서 다음 폴링에 다시 와요(중복은 소비자가
        `eventId` 로 걸러요).

        ⚠️ 되떨어지는 경우 하나: 남긴 것 **전부가 같은 밀리초** 면 완결된 경계가 없어요.
        그때는 그냥 `limit` 을 돌려줘요 — 안 그러면 0건이 되고 커서가 멈춰서 같은 구간을
        영원히 다시 읽어요. 한 밀리초에 `limit` 건(런타임 로그는 1,000건)이 찍혀야 하는
        조건이라, 교착보다 이쪽이 안전해요.
        """
        kept = min(len(timestamps), limit)
        if kept == 0 or not more_available:
            return kept
        boundary = timestamps[kept - 1]
        if boundary is None:
            return kept
        # 상한 **밖** 을 볼 수 있으면 먼저 확인해요 — 그룹이 안 이어지면 물러날 필요가 없어요.
        if len(timestamps) > kept and timestamps[kept] != boundary:
            return kept
        keep = kept - 1
        while keep > 0 and timestamps[keep - 1] == boundary:
            keep -= 1
        return keep or kept       # 전부 같은 밀리초면 멈추지 않는 쪽을 골라요
