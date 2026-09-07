"""거버넌스 스캔 폴러 — running 스캔을 주기 순회해 SF 완료를 done으로 당기고 verdict를 적용.

deploy poller.py 형제. sync_fn(record_id)은 sync_running_stepfn + apply_verdict_to_registry를
묶은 것(둘 다 멱등). 그래서 멀티인스턴스가 동시에 돌아도 결과가 같아요(락 불필요).
배포형 자동스캔 유실(결함 A)·SF 완료 pull-only(결함 B)를 서버측 push로 해소해요.
"""
from __future__ import annotations

import asyncio
import logging

_log = logging.getLogger(__name__)


def running_record_ids(store) -> list[str]:
    """store에서 최신 스캔 status가 'running'인 record_id 목록.

    DynamoGovStore는 SCAN# 파티션을 scan해 running만 골라요. store에 running_record_ids가
    있으면 그걸 쓰고(구현 최적화 여지), 없으면 all_scans 기반 폴백은 각 store가 제공.
    """
    if hasattr(store, "running_record_ids"):
        return store.running_record_ids()
    return []


def done_record_ids(store) -> list[str]:
    """store에서 최신 스캔 status가 'done'인 record_id 목록(verdict backstop 후보).

    running_record_ids의 형제. store에 done_record_ids가 없으면 빈 목록.
    """
    if hasattr(store, "done_record_ids"):
        return store.done_record_ids()
    return []


def pending_verdict_record_ids(
    *, done_ids, status_of, is_terminal, settled, running_ids=(),
) -> list[str]:
    """done 스캔인데 registry가 아직 non-terminal인 record_id 목록(verdict 재적용 대상).

    결함(고착): stepfn 스캔에서 aggregate Lambda가 "done"을 push하면 gov 폴러가 그 레코드를
    running 창에서 놓쳐 `apply_verdict_to_registry`를 못 불러 PENDING에 갇혀요. 이 backstop이
    done인데 non-terminal인 레코드를 골라 verdict를 재적용(멱등)하게 해요.

    - `running_ids`로 받은(=재스캔으로 다시 running이 된) 레코드는 `settled`에서 풀어요(re-arm).
      재스캔은 registry를 다시 PENDING으로 리셋하므로(APPROVED 자산도), 예전에 terminal로
      settle된 record_id를 그대로 두면 새 스캔의 verdict가 영영 재적용 안 돼 재고착돼요(M1).
      스캔은 실행 내내 running이라 폴링 주기마다 관측돼 안정적으로 re-arm돼요.
    - terminal(APPROVED/REJECTED/DEPRECATED)은 `settled`에 넣어 다음부터 조회하지 않아요(바운드).
    - `status_of`가 None을 주면 영구 부재(삭제된 레코드)로 보고 settle해요(무한 재시도 방지).
    - `status_of` 조회가 예외를 던지면(일시 오류) settle하지 않고 건너뛰어 다음 주기에 재시도해요.
    - registry 의존(status_of·is_terminal)은 주입 — 폴러 로직을 순수하게 단위 테스트하려고요.
    """
    for rid in running_ids:
        settled.discard(rid)  # 재스캔 재무장 — 새 스캔 verdict를 다시 평가
    out: list[str] = []
    for rid in done_ids:
        if rid in settled:
            continue
        try:
            status = status_of(rid)
        except Exception:
            continue  # 일시 조회 실패 — settle 안 하고 다음 주기 재시도
        if status is None or is_terminal(status):
            settled.add(rid)  # terminal이거나 영구 부재 — 더 안 봐요(바운드)
        else:
            out.append(rid)
    return out


async def gov_poll_once(store, *, sync_fn, running_ids_fn) -> int:
    """running 스캔을 각 sync_fn으로 전진. 전진 수 반환. 개별 실패는 격리."""
    advanced = 0
    for rid in running_ids_fn():
        try:
            await asyncio.to_thread(sync_fn, rid)
            advanced += 1
        except Exception:
            _log.exception("gov poller sync failed: %s", rid)
    return advanced


async def run_gov_poller(*, interval, sleep, should_run, sync_fn, running_ids_fn) -> None:
    """should_run()이 True인 동안 gov_poll_once → sleep(interval) 반복."""
    while should_run():
        try:
            await gov_poll_once(None, sync_fn=sync_fn, running_ids_fn=running_ids_fn)
        except Exception:
            _log.exception("gov poller cycle failed")
        await sleep(interval)
