"""등록 라이프사이클 hook 구현 — `shared.governance.RegistrationGate` (ADR-017).

등록 경로가 `create_record`로 DRAFT 레코드를 만든 직후, 성공 응답과 자동 스캔 시작 전에
정확히 한 곳에서 호출돼요. 하는 일은 두 단계예요.

1. **최소 PENDING_APPROVAL 확정** — DRAFT는 내부 과도 상태예요(ADR-017 결정 2). 응답이
   나가기 전에 `submit_for_approval`로 심사 착수를 기록해요. 이걸 안 하면 응답 직후
   자산이 DRAFT에 머무는 창이 생기고, 그 창에서 자산은 검토 큐엔 있지만 어떤 판정도
   받지 않은 상태로 방치돼요.
2. **이미 확정된 스캔 verdict 반영** — 그 자산에 완료된 스캔이 있으면 기존 verdict 경로
   (`apply_verdict_to_registry`)가 최종 상태를 정해요. 미스캔·미확정이면 전이하지 않고
   `PENDING_REVIEW`로 남겨 검토 큐가 집어가요(ADR-017 결정 3·6).

**승인 폴백 없음.** registry 조회·전이가 실패하면 `PENDING_REVIEW`를 돌려요. 실패를
`APPROVED`로 접으면 게이트가 장애 시 자동 통과기가 되니까요(ADR-017 결정 3).

verdict는 입력으로 받지 않고 `GovStore`의 최신 스캔에서 계산해요. trust adapter는 같은
데이터를 읽는 read model일 뿐이라 등록 판정의 입력으로 역참조하지 않아요.
"""
from __future__ import annotations

import logging
import time

from ...shared.governance import (
    GovernanceDecision,
    RegistrationOutcome,
    RegistrationRequest,
)


_log = logging.getLogger(__name__)

# 등록 응답 안에서 끝나야 하므로 재시도는 짧게만 해요.
#
# **이 예산은 재시도 횟수의 상한이지 wall-clock 상한이 아니에요.** 명시적 sleep 합계는
# 0.3초이고, deadline은 "다음 시도를 시작할지"만 판단해요 — 이미 시작된 boto3 호출을
# 중단시키지는 못해요(botocore 내부 retry·socket timeout은 클라이언트 설정 소관). 그래서
# control plane이 매우 느리면 등록 응답도 그만큼 느려질 수 있어요. 진짜 wall-clock 상한이
# 필요하면 registry 클라이언트에 connect/read timeout을 설정해야 하고, 그건 이 게이트만의
# 결정이 아니라 공용 어댑터 계약이라 별도 작업으로 남겼어요.
_SUBMIT_ATTEMPTS = 3
_SUBMIT_BACKOFF_SECONDS = 0.05
_SUBMIT_DEADLINE_SECONDS = 5.0


class GovernanceRegistrationGate:
    """RegistryPort·GovStore만 통해 상태를 바꾸는 등록 게이트.

    Registry 테이블이나 DynamoDB를 직접 쓰지 않아요 — catalog 상태는 포트로만 바꾼다는
    협업 규칙(`docs/04-collaboration.md`)을 지켜요.
    """

    def __init__(
        self, registry, registry_id: str, store, *, sleep=None, monotonic=None
    ) -> None:
        self._registry = registry
        self._registry_id = registry_id
        self._store = store
        # 테스트가 재시도 대기·deadline을 제어할 수 있게 주입 가능하게 둬요.
        self._sleep = sleep or time.sleep
        self._monotonic = monotonic or time.monotonic

    def on_registered(self, request: RegistrationRequest) -> RegistrationOutcome:
        from ..catalog.registry.models import RecordStatus

        record_id = (request.record_id or "").strip()
        if not record_id:
            # record_id 없이 판정할 방법이 없어요. 통과시키지 않고 검토로 넘겨요.
            return RegistrationOutcome(GovernanceDecision.PENDING_REVIEW)

        status = self._ensure_pending(record_id)
        if status is None:
            # 조회·전이 실패 — 상태를 확정하지 못했으니 승인으로 접지 않아요.
            return RegistrationOutcome(GovernanceDecision.PENDING_REVIEW)

        # 완료된 스캔이 있으면 기존 verdict 경로가 최종 상태를 정해요(단일 판정 로직 재사용).
        final = self._apply_settled_verdict(record_id, status)
        if final is RecordStatus.APPROVED:
            return RegistrationOutcome(GovernanceDecision.APPROVED, final.value)
        if final is RecordStatus.REJECTED:
            return RegistrationOutcome(GovernanceDecision.REJECTED, final.value)
        return RegistrationOutcome(GovernanceDecision.PENDING_REVIEW, final.value)

    # ── 내부 ──────────────────────────────────────────────────────────
    def _ensure_pending(self, record_id: str):
        """DRAFT면 submit_for_approval로 승격. 반환은 확정된 현재 상태(실패 시 None).

        멱등: 이미 PENDING/APPROVED/REJECTED면 전이하지 않고 그 상태를 그대로 돌려줘요.
        등록 경로가 중복 호출돼도(재시도·poller 반복) 상태를 되돌리지 않아요.

        AgentCore control plane의 일시 오류(throttling 등)로 등록이 조용히 DRAFT에
        남지 않도록 제한 재시도를 해요. 등록 요청의 동기 응답 안에서 끝나야 하니 짧게만
        시도하고, 그래도 확정 못 하면 None을 돌려 호출부가 승인으로 접지 않게 해요.
        """
        from ..catalog.registry.models import RecordStatus

        last_error: Exception | None = None
        deadline = self._monotonic() + _SUBMIT_DEADLINE_SECONDS
        for attempt in range(_SUBMIT_ATTEMPTS):
            if attempt and self._monotonic() >= deadline:
                # SDK 호출이 느려 전체 예산을 넘겼어요. 더 기다리면 등록 응답만 늦어져요.
                break
            try:
                current = self._registry.get_record(self._registry_id, record_id).status
            except Exception as exc:
                last_error = exc
                self._sleep(_SUBMIT_BACKOFF_SECONDS * (attempt + 1))
                continue
            if current is not RecordStatus.DRAFT:
                return current
            try:
                # 실 AgentCore 제약: DRAFT→PENDING_APPROVAL은 전용 submit API로만 가능해요
                # (`registry/aws_adapter.py`의 update_status 주석 참조).
                return self._registry.submit_for_approval(self._registry_id, record_id)
            except Exception as exc:
                last_error = exc
                self._sleep(_SUBMIT_BACKOFF_SECONDS * (attempt + 1))
        # 확정 실패를 조용히 넘기지 않아요 — 자산이 DRAFT에 남았다는 사실이 운영에 보여야
        # reviewer 수동 판정(self-heal) 전까지 방치되지 않아요.
        _log.warning(
            "registration hook could not confirm PENDING_APPROVAL: record_id=%s error=%s",
            record_id,
            type(last_error).__name__ if last_error else "unknown",
        )
        return None

    def _apply_settled_verdict(self, record_id: str, status):
        """완료 스캔이 있으면 verdict를 반영하고 최종 상태를 반환해요.

        스캔이 없거나 아직 done이 아니면 전이 없이 현재 상태를 그대로 돌려줘요 —
        미스캔 자산을 "판정 없음"이라는 이유로 승인하지 않아요.
        """
        from ..catalog.registry.models import RecordStatus
        from .scan_service import apply_verdict_to_registry

        if status in (RecordStatus.APPROVED, RecordStatus.REJECTED):
            return status
        try:
            scan = self._store.latest_scan(record_id)
        except Exception:
            return status
        if scan is None or getattr(scan, "status", "") != "done":
            return status
        try:
            apply_verdict_to_registry(record_id)
            return self._registry.get_record(self._registry_id, record_id).status
        except Exception:
            # verdict 반영 실패는 흡수하되 승인으로 올리지 않아요 — 다음 폴링이 재시도해요.
            return status
