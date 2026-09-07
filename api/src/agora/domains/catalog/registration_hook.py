"""등록 라이프사이클 hook 호출 지점 (ADR-017 결정 1).

모든 등록 경로가 `create_record` **직후**, 성공 응답과 자동 스캔 시작 **전에** 이 한 함수를
거쳐요. 경로마다 인라인으로 게이트를 부르던 이전 방식은 `record_id`도 정규화된 타입·버전도
없는 시점에 판정해서, 실제로 5개 경로 중 2개에만 적용돼 있었어요(ADR-017 배경).

호출 순서가 중요해요:

    create_record  →  on_registered(hook)  →  maybe_auto_scan  →  성공 응답

hook이 먼저여야 응답 시점에 레코드가 최소 `PENDING_APPROVAL`로 확정돼요. 스캔을 먼저 걸면
스캔 시작 전이와 hook 전이가 경쟁하고, 응답 직후 자산이 DRAFT인 창이 남아요.

**반려 응답 계약:** 반려도 durable record로 남기고 HTTP는 403이에요. 레코드를 지우지 않는
이유는 거버넌스 감사에 "무엇이 왜 반려됐는지"가 남아야 하기 때문이고, 403인 이유는 기존
연결형 등록 클라이언트가 반려를 실패로 다루도록 이미 작성돼 있어서예요(성공 200에
`REJECTED`를 실으면 그 클라이언트가 반려 자산을 등록 성공으로 취급해요).
"""
from __future__ import annotations

import logging

from fastapi import HTTPException

from ...shared.governance import (
    GovernanceDecision,
    RegistrationRequest,
    RegistrationTrigger,
)

_log = logging.getLogger(__name__)


def run_registration_hook(
    record_id: str,
    principal: str,
    trigger: RegistrationTrigger,
    *,
    raise_on_rejected: bool = True,
) -> str:
    """등록 hook을 실행하고 확정된 Registry 상태 문자열을 돌려줘요.

    `raise_on_rejected=True`(등록 API 기본)면 반려 시 403을 던져요. 레코드는 이미
    REJECTED로 남아 있으니 감사 기록은 유지돼요.

    배포 완료 후 등재처럼 HTTP 응답이 없는 경로는 `raise_on_rejected=False`로 불러요 —
    거기서 예외를 던지면 이미 만들어진 AWS 리소스를 남기고 배포 폴링만 깨져요.

    **상태를 확정하지 못하면 동기 등록은 503이에요.** 재시도까지 실패했는데 200을 돌려주면
    호출자는 등록이 정상이라고 믿는데 자산은 DRAFT에 남아요(`DRAFT -> APPROVED/REJECTED`가
    불법 전이라 reviewer 결정도 삼켜져요). ADR-017 결정 2의 "응답 전 최소
    `PENDING_APPROVAL`"을 지키려면 확정 실패는 실패로 보고해야 해요. 레코드는 남아 있으니
    호출자가 같은 이름으로 재시도하면 unique key 충돌을 만나요 — 그래서 `record_id`를 함께
    돌려줘 정리할 수 있게 해요.

    hook 자체가 예외로 터지면 **등록을 승인으로 접지 않고** 그대로 올려보내요.
    """
    from ...shared.deps import (
        get_mcp_reregistration_cleanup,
        get_registration_gate,
    )

    # 정상 재등록 승인은 exact old record를 가진 purge가 회수해요. 이 hook은
    # legacy/race Target 충돌을 관측하되 자산 동일성을 추론하거나 삭제하지 않아요.
    try:
        report = get_mcp_reregistration_cleanup().reconcile(
            record_id,
            actor=principal,
        )
        if report.unknown:
            _log.warning(
                "MCP re-registration approval cleanup is unknown; "
                "replacement_record_id=%s coordinates=%s",
                record_id,
                report.unknown,
            )
    except Exception:  # noqa: BLE001 - cleanup never blocks registration.
        _log.exception(
            "MCP re-registration approval cleanup failed; "
            "replacement_record_id=%s actor=%s",
            record_id,
            principal,
        )

    outcome = get_registration_gate().on_registered(
        RegistrationRequest(record_id=record_id, principal=principal, trigger=trigger)
    )
    if not outcome.status and raise_on_rejected:
        raise HTTPException(
            503,
            {
                "message": (
                    "등록 심사 상태를 확정하지 못했어요. 잠시 후 다시 시도해 주세요."
                ),
                "record_id": record_id,
                "remediation": (
                    "이 레코드는 아직 심사 대기로 전환되지 않았어요. "
                    "재시도 전에 남은 레코드를 삭제하거나 새 버전을 쓰세요."
                ),
            },
        )
    if outcome.decision is GovernanceDecision.REJECTED and raise_on_rejected:
        # record_id를 함께 돌려줘요. Registry의 unique key는 `name + recordVersion`이라
        # REJECTED 레코드가 남으면 같은 이름·버전으로 재등록할 때 충돌해요. 호출자가
        # 반려된 레코드를 찾아 삭제하거나 버전을 올릴 수 있게 식별자를 주는 거예요
        # (일반 403 메시지만 주면 소유자가 스스로 복구할 방법이 없어요).
        raise HTTPException(
            403,
            {
                "message": "거버넌스 게이트에서 반려됐어요.",
                "record_id": record_id,
                "remediation": (
                    "반려된 레코드를 삭제하거나 새 버전으로 다시 등록해 주세요."
                ),
            },
        )
    return outcome.status
