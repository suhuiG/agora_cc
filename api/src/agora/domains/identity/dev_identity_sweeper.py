"""Background cleanup for expired dev credentials and their Cedar permits.

이 pass 는 정리만 하는 게 아니에요 — `sweep_expired()` 가 공유 ① Cedar 정책의 **수렴**도
맡아요(IH-160 / ADR-0110). 발급·회수의 재프로비저닝이 미수렴으로 끝나면 그 도구가
`tools/list` 에서 사라진 상태로 남는데, IH-154 수렴 pass 는 prune 전용이라 빠진 열거를
만들어 주지 않거든요(ADR-0107 결정 4). 그래서 재시도 좌표를 이 주기가 이어받아요.
"""
from __future__ import annotations

import asyncio
import logging

_log = logging.getLogger(__name__)


async def run_dev_identity_sweeper(
    service,
    *,
    interval: float,
    sleep,
    should_run,
) -> None:
    while should_run():
        try:
            await asyncio.to_thread(service.sweep_expired)
        except Exception:
            _log.exception("dev identity expiry sweep failed")
        await sleep(interval)
