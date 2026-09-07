"""dev identity Cedar policy 의 PENDING→ACTIVE 승격과 옛 revision 회수를 도는 배경 루프.

IH-78: 코드 다운로드는 Cedar 활성화를 기다리지 않아요(실측 8.5~72.6초, 증가 추세이고
배포 포털은 ALB idle timeout 에도 걸려요). 그래서 발급 직후 원장은 PENDING 이고, **실제
활성화를 관측해 원장을 맞추는 주체**가 필요해요. 없으면 원장이 영구히 PENDING 이라
거짓말을 해요(ADR-0037 §4).

같은 이유로 **옛 revision 회수도 이 루프가 해요.** 발급 경로는 PENDING 을 받고 돌아오니
`AgentPolicyDeployer.deploy` 안의 정리 지점을 지나가지 않고, ACTIVE 를 관측하는 주체는 여기
뿐이에요. Cedar ACTIVE permit 은 합집합이라 옛 revision 이 남으면 도구 회수가 무효예요.

만료 sweeper(`dev_identity_sweeper`)와 분리한 이유: 그쪽은 크리덴셜 수명이 만든 자원을
통째로 **지우는** 작업이라 공유 테이블 정렬 가드로 막혀 있어요(IH-26). 이 루프도 이제
superseded 정책을 지우지만 그 가드를 붙이지 않아요 — 권한 회수가 반영되는지는 옵션
스위치에 걸릴 수 없고, 대상이 "원장이 소유를 확인한, 최신보다 낮은 revision" 하나로
좁혀져 있으며 삭제가 멱등이라 두 프로세스가 같이 돌아도 결과가 같아요.
"""
from __future__ import annotations

import asyncio
import logging

_log = logging.getLogger(__name__)


async def run_dev_policy_reconciler(
    service,
    *,
    interval: float,
    sleep,
    should_run,
    limit: int = 20,
) -> None:
    while should_run():
        try:
            counts = await asyncio.to_thread(
                service.reconcile_pending_policies, limit=limit
            )
            if (
                counts.get("activated")
                or counts.get("failed")
                or counts.get("reclaimed")
                or counts.get("reclaim_failed")
            ):
                _log.info(
                    "dev identity policy reconcile: checked=%s activated=%s "
                    "failed=%s pending=%s reclaimed=%s reclaim_failed=%s",
                    counts.get("checked", 0),
                    counts.get("activated", 0),
                    counts.get("failed", 0),
                    counts.get("pending", 0),
                    counts.get("reclaimed", 0),
                    counts.get("reclaim_failed", 0),
                )
        except Exception:
            _log.exception("dev identity policy reconcile failed")
        await sleep(interval)
