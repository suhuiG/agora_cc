"""백그라운드 폴러 — 미종료 배포 job을 주기적으로 전진시켜요.

서버 stateless poll-driven 구조에서, 사용자가 화면을 떠나도 배포가 완료되도록
lifespan이 이 run_poller를 asyncio 태스크로 띄워요. poll()은 blocking(boto3)이라
asyncio.to_thread로 감싸 이벤트 루프를 막지 않아요. 매 전진 후 대응 요청 로그를 sync.
"""
from __future__ import annotations

import asyncio
import logging

from ....domains.catalog.requests.store import sync_from_job
from .models import DeployPhase, terminal

_log = logging.getLogger(__name__)

# 동시 poll 가드 — 프론트 GET poll과 백그라운드 폴러가 같은 job을 동시에 전진시키면
# create_agent_runtime이 두 번 불려 경쟁조건(고아 runtime·running 갇힘)이 생겨요.
# 진행 중인 job_id를 여기 담아, 이미 전진 중이면 이번 순회에선 건너뛰어요.
_in_flight: set[str] = set()

# terminal job의 governance 복구 상태예요. 확정된 레코드만 영구 제외하고,
# DRAFT·조회 실패는 아래 backoff 일정에 따라 계속 재시도해요.
# 시도 횟수가 늘면 다음 검사 시점을 미뤄요(backoff). **영구 포기는 안 해요** — 일시
# 장애(throttling·5xx)가 오래 이어진 뒤 정상화되면 복구돼야 하니까요.
#
# **job별 `next_retry_cycle`을 쓰고 due job은 오래 기다린 순서로 처리해요.** 전역
# `cycle % interval == 0` 방식은 starvation을 만들었어요: 오래 실패한 job이 모두 같은
# 간격(상한)으로 수렴하면 같은 주기에 한꺼번에 due가 되고, 주기당 예산(5건)을 리스트 앞쪽이
# 매번 소진해서 뒤쪽 job은 영구히 차례가 오지 않았어요(실측: 512주기 이후 뒤 5개 재시도 0회).
_governance_settled: set[str] = set()          # 확정 확인됨 → 다시 안 봄
_governance_attempts: dict[str, int] = {}      # job_id → 미확정 시도 횟수
_governance_next_cycle: dict[str, int] = {}    # job_id → 다음 검사 가능 주기
# 간격은 선형으로 늘려요: 시도 n회 → n*2 주기 뒤(상한 32).
# 지수(2^n)는 몇 번만 실패해도 간격이 수백 주기로 뛰어 복구가 과도하게 늦어져요.
_GOVERNANCE_BACKOFF_MAX_CYCLES = 32
# 이 횟수를 넘으면 ERROR로 운영 개입을 요청해요(포기가 아니라 알림).
_GOVERNANCE_ALERT_AFTER_ATTEMPTS = 10
# 한 주기에 검사할 terminal job 상한 — 첫 주기에 몰려서 폭주하지 않게 해요.
_GOVERNANCE_RETRY_PER_CYCLE = 5
_governance_cycle = 0                          # poll_once 순회 카운터(backoff 기준)


def _reset_governance_retry_state() -> None:
    """테스트 전용 — 재시도 가드를 비워요."""
    global _governance_cycle
    _governance_settled.clear()
    _governance_attempts.clear()
    _governance_next_cycle.clear()
    _governance_cycle = 0


def _governance_due(job_id: str) -> bool:
    """예약된 재시도 주기에 도달했는지. 이력이 없으면 즉시 대상이에요."""
    return _governance_cycle >= _governance_next_cycle.get(job_id, 0)


def _schedule_next_governance_retry(job_id: str) -> None:
    """시도 횟수에 따라 다음 검사 주기를 예약해요(선형 backoff)."""
    attempts = _governance_attempts.get(job_id, 0)
    delay = min(max(attempts, 1) * 2, _GOVERNANCE_BACKOFF_MAX_CYCLES)
    _governance_next_cycle[job_id] = _governance_cycle + delay


def _governance_candidates(jobs: list) -> list:
    """검사 대상 terminal job을 **오래 기다린 순서로** 정렬해요.

    같은 주기에 여러 job이 due여도 예산(5건)이 앞쪽만 먹지 않게, 예약 주기가 이른(=오래
    기다린) job을 먼저 줘요. 그래서 어떤 job도 영구히 밀리지 않아요.
    """
    return sorted(jobs, key=lambda j: _governance_next_cycle.get(j.job_id, 0))


def _retry_pending_governance(job) -> bool:
    """terminal job의 미완 governance 후처리를 재시도해요(멱등). 검사했으면 True.

    READY로 끝난 배포 중 레코드 상태가 아직 `DRAFT`인 것만 대상이에요. `DRAFT`는 내부 과도
    상태라(ADR-017 결정 2) 거기 남으면 `DRAFT -> APPROVED/REJECTED`가 불법 전이여서 reviewer
    결정까지 삼켜져요. 조회 실패·이미 확정된 레코드는 아무 일도 하지 않아요.
    """
    if job.phase is not DeployPhase.READY:
        return False
    record_id = getattr(job, "record_id", None)
    if not record_id or job.job_id in _governance_settled:
        return False
    if not _governance_due(job.job_id):
        return False
    from ....domains.catalog.registry.models import RecordStatus
    from ....shared.deps import get_registry, get_registry_id

    # 조회 성공 시점에 검사 완료로 표시해요. 조회 자체가 실패하면 표시하지 않아 다음 주기에
    # 다시 시도해요(일시 장애 구제).
    try:
        status = get_registry().get_record(get_registry_id(), record_id).status
    except Exception:
        _governance_attempts[job.job_id] = _governance_attempts.get(job.job_id, 0) + 1
        _schedule_next_governance_retry(job.job_id)
        return True                       # 이번 주기 예산은 소비했어요(폭주 방지)
    if status is not RecordStatus.DRAFT:
        # 확정됐어요(PENDING/APPROVED/REJECTED) — 다시 볼 필요 없어요.
        # 재시도 부기(attempts·next_cycle)는 버려요. terminal job은 지워지지 않고 쌓이니
        # 확정된 것까지 dict에 남겨두면 프로세스 수명 내내 항목이 계속 늘어나요.
        _governance_settled.add(job.job_id)
        _governance_attempts.pop(job.job_id, None)
        _governance_next_cycle.pop(job.job_id, None)
        return True
    _governance_attempts[job.job_id] = _governance_attempts.get(job.job_id, 0) + 1
    _schedule_next_governance_retry(job.job_id)
    _log.warning(
        "deploy record still DRAFT after terminal phase — retrying governance: "
        "job_id=%s record_id=%s", job.job_id, record_id,
    )
    try:
        from ....domains.governance.auto_scan import process_deploy_governance
        process_deploy_governance(job)
    except Exception:
        _log.exception("governance retry failed: job_id=%s", job.job_id)
    attempts = _governance_attempts.get(job.job_id, 0)
    if attempts >= _GOVERNANCE_ALERT_AFTER_ATTEMPTS:
        # 재시도는 계속하지만(포기 아님) 운영 개입이 필요한 상태예요.
        _log.error(
            "governance retry still failing after %d attempts — record stays DRAFT, "
            "backing off: job_id=%s record_id=%s",
            attempts, job.job_id, record_id,
        )
    return True


async def poll_once(deploy_service, request_log, *, now) -> int:
    """미종료 job을 각 1단계 전진시키고 요청 로그를 sync. 전진시킨 job 수 반환.

    같은 job이 다른 poll_once에서 이미 전진 중이면(=_in_flight) 건너뛰어 중복 전진을 막아요.
    """
    global _governance_cycle
    _governance_cycle += 1
    advanced = 0
    governance_budget = _GOVERNANCE_RETRY_PER_CYCLE
    all_jobs = deploy_service.store.list()
    # non-terminal 전진은 원래 순서 그대로예요(배포 진행 순서를 바꿀 이유가 없어요).
    # terminal job의 governance 복구만 "오래 기다린 순서"로 따로 돌려요 — 예산(5건)을
    # 리스트 앞쪽이 매번 소진해 뒤쪽이 영구히 밀리는 starvation을 막아요.
    for job in _governance_candidates([j for j in all_jobs if terminal(j.phase)]):
        if governance_budget <= 0:
            break
        if _retry_pending_governance(job):
            governance_budget -= 1

    for job in all_jobs:
        if terminal(job.phase):
            continue                      # governance 복구는 위 전용 루프가 담당해요
        if job.job_id in _in_flight:   # 다른 poll이 이 job을 전진 중 — 중복 방지
            continue
        _in_flight.add(job.job_id)
        try:
            fresh = await asyncio.to_thread(deploy_service.poll, job.job_id)
        finally:
            _in_flight.discard(job.job_id)
        if fresh is not None:
            sync_from_job(request_log, fresh, now=now)
            # 배포 완료 governance 후처리. 프론트 poll에 의존하지 않고 서버측 폴러도
            # 같은 멱등 함수를 호출해 Initializr 스캔 생략/일반 배포 스캔을 일관되게 적용해요.
            if (
                getattr(fresh, "record_id", None)
                and fresh.phase is DeployPhase.READY
            ):
                from ....domains.governance.auto_scan import process_deploy_governance
                process_deploy_governance(fresh)
            advanced += 1
    return advanced


async def run_poller(deploy_service, request_log, *, now, interval, sleep, should_run) -> None:
    """should_run()이 True인 동안 poll_once → sleep(interval)을 반복해요."""
    while should_run():
        try:
            await poll_once(deploy_service, request_log, now=now)
        except Exception:
            # 한 순회 실패가 폴러 전체를 죽이지 않게 흡수(다음 주기에 재시도).
            _log.exception("poller cycle failed")
        await sleep(interval)
