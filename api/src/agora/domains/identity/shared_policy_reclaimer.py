"""공유 ① Cedar 정책의 옛 ACTIVE 리비전을 주기적으로 회수해요 (IH-154).

## 왜 폴러여야 하나

`SharedPolicyProvisioner.provision()` 은 새 리비전이 전부 ACTIVE 가 된 **뒤에만** 옛
리비전을 지워요. 순서를 뒤집으면 새 정책이 `CREATING` 인 4~5초 동안 전면 거부 창이
열리거든요. 그래서 활성화가 관측 예산을 넘기면(`PolicyActivationPending`) 새 리비전과 옛
리비전이 **함께 ACTIVE** 로 남아요. Cedar permit 은 합집합이라 가장 넓은 옛 리비전이 계속
이기고, 그게 「다음 회수를 무력화한다」예요 — 어드민이 도구를 회수해도 실제로 좁아지지 않아요.

HTTP 요청은 이 수렴을 맡을 수 없어요. 어드민 경로는 ALB 수명 때문에 8폴로 잘려 있고
(`shared_policy_trigger.HTTP_ACTIVE_MAX_POLLS`), purge 는 60폴을 요청 안에서 동기 대기하다가
CloudFront 30초 read timeout 에 걸려요. 그래서 요청을 붙잡지 않고 폴러가 이어받아요.

## 파괴적이에요

이 pass 는 라이브 Cedar 정책을 **지워요.** `server._start_poller_tasks` 안에 등록해
`AGORA_ROLE` 소유권 규칙과 종료 취소를 그대로 받고, 그 위에 정렬 가드를 하나 더 겁니다
(`server._shared_policy_reclaimer_enabled`) — 비표준 identity 테이블이나 좌표가 없는
환경에서 공유 정책을 지우지 않게요.
"""
from __future__ import annotations

import asyncio
import logging

_log = logging.getLogger(__name__)


async def run_shared_policy_reclaimer(
    reclaim,
    *,
    interval: float,
    sleep,
    should_run,
) -> None:
    """`reclaim()` 을 주기적으로 불러요. 실패는 삼키고 다음 주기에 재시도해요.

    `reclaim` 은 `dict` 리포트를 돌려주는 무인자 호출이에요
    (`deps.reclaim_stale_shared_gateway_policy_revisions`). 던지는 것과 `ok=False` 를
    구분해요 — 후자는 provisioner 가 이미 「아무것도 지우지 않았다」를 판정한 결과예요.
    """
    while should_run():
        try:
            report = await asyncio.to_thread(reclaim)
        except Exception:
            _log.exception("shared policy stale-revision reclaim failed")
        else:
            verdict = str((report or {}).get("verdict") or "")
            # `not_applicable` 은 정상 상태(리비전 0장 또는 1장)라 매 주기 로그를 남기지
            # 않아요. 나머지는 남겨요 — `blocked`·`unknown` 은 통과가 아니에요.
            if verdict != "not_applicable":
                _log.info("shared policy reclaim report=%s", report)
        await sleep(interval)
