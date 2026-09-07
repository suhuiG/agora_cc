"""MCP 도구 목록 드리프트 주기 폴러.

관리자 "다시 읽기"와 등록 훅만으로는 부족해요 — MCP 개발자는 우리 통제 밖이고 조용히
도구를 바꿔요. 그래서 주기 관측이 필수예요.

연결형 MISSING을 찾으면 Target을 명시 동기화하고 완료와 후속 부재를 검증해요. 실패는
삼키고 다음 주기에 재시도하며, 검증하지 않은 상태를 `RETIRED`로 접지 않아요.
끄는 스위치는 `AGORA_MCP_DRIFT_POLL_ENABLED`(`[E]`)예요.
"""
from __future__ import annotations

import asyncio
import logging

_log = logging.getLogger(__name__)


async def run_mcp_drift_poller(service, *, interval: float, sleep, should_run) -> None:
    while should_run():
        try:
            snapshots = await asyncio.to_thread(service.resync_all)
            drifted = [s for s in snapshots if s.has_drift]
            unknown = [s for s in snapshots if s.check_status.value == "unknown"]
            if drifted or unknown:
                _log.info(
                    "mcp tool drift: assets=%s drifted=%s unobserved=%s",
                    len(snapshots),
                    len(drifted),
                    len(unknown),
                )
        except Exception:
            _log.exception("mcp tool drift poll failed")
        await sleep(interval)
