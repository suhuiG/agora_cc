"""게시 완료 훅 — 설정이 켜져 있을 때만 자동 스캔 (§2.5 auto 트리거).

catalog 게시 경로(register + deploy READY 등재)가 record 생성 후 이 훅을 호출.
기본 OFF라 아무 일도 안 함. 비차단: 스캔 실패가 게시를 막지 않도록 예외를 삼켜요(감사엔 남음).

멱등: 이미 스캔 레코드가 있으면 재스캔하지 않아요. deploy 경로는 poll이 READY를
여러 번 반환할 수 있어(폴링) 가드가 없으면 매 poll마다 중복 스캔이 돌거든요.
재배포는 코드가 바뀌었으니 예외 — for_version으로 그 버전 스캔이 없으면 다시 돌려요.
"""
from __future__ import annotations

import logging

from ...shared.deps import get_gov_store, get_scan_service

_log = logging.getLogger(__name__)


def maybe_auto_scan(record_id: str, *, for_version: str = "") -> bool:
    """자동 스캔을 (조건이 맞으면) 실행해요. 실행했으면 True.

    for_version을 주면 "최신 스캔이 이 버전을 봤는가"로 판단해요. 재배포용이에요 —
    코드가 바뀌었는데 "스캔 기록이 있다"는 이유로 건너뛰면 새 코드가 무검사로
    통과하고, UpdateRegistryRecord가 되돌린 승인 상태도 복구되지 않아요
    (실측 2026-07-27: 재배포 후 자산이 DRAFT로 남아 카탈로그에서 사라졌어요).
    """
    store = get_gov_store()
    if not store.get_settings().auto_scan:
        return False
    latest = store.latest_scan(record_id)
    if not _should_scan(latest, for_version):
        return False
    try:
        get_scan_service().run(record_id, trigger="auto", principal="system")
    except Exception:
        return False
    return True


def process_deploy_governance(job) -> bool:
    """배포 완료 후 출처에 맞는 governance 처리를 수행해요.

    Initializr는 플랫폼이 생성한 표준 scaffold를 그대로 배포하므로 security scan
    pipeline을 실행하지 않고 직접 승인해요. 그 외 등록/배포는 등록 hook(ADR-017) →
    기존 auto-scan 정책 순서로 적용합니다. 호출부가 프론트 poll과 서버 poller 두 곳이라
    멱등이어야 해요.
    """
    phase = getattr(job, "phase", None)
    if getattr(phase, "value", phase) != "READY":
        return False

    from .post_registration import run_post_registration, run_overlap_only

    for_version = (
        job.source_ref.version if getattr(job, "is_redeploy", False) else ""
    )
    if (
        getattr(job, "asset_type", "") == "agent"
        and (getattr(job, "meta", {}) or {}).get("deployment_source") == "initializr"
    ):
        approved = _approve_initializr_without_scan(job.record_id)
        # **security scan만** 건너뛰어요. 중복검토는 Initializr 자산도 받아야 해요 — 플랫폼이
        # 만든 scaffold라고 해서 "같은 일을 하는 자산이 이미 있는지"가 면제되는 건 아니에요.
        # 여기서 함께 부르지 않으면 Initializr 경로가 영구히 `not_reviewed`로 남아요
        # (단일 진입점 계약의 구멍).
        #
        # `maybe_overlap_review`를 직접 부르지 않고 같은 격리 헬퍼를 거쳐요 — 그 함수의
        # `get_gov_store()`·`get_settings()`·`latest_overlap()`은 내부 try 바깥이라, 직접 부르면
        # GovStore 일시 실패가 배포 poll 응답을 500으로 만들어요. Registry는 이미 APPROVED인데
        # poll만 깨지고, 서버 poller는 "DRAFT가 아니다"라며 settled 처리해 재시도도 안 해요.
        run_overlap_only(job.record_id, for_version=for_version)
        return approved
    # 배포 완료 후 Registry 등재도 같은 등록 hook을 거쳐요(ADR-017 결정 1). auto_scan이
    # 꺼져 있어도 레코드가 DRAFT에 고립되지 않고 최소 PENDING_APPROVAL로 확정돼요.
    _run_registration_hook(job)
    _seed_mcp_tool_drift(job)
    # 후처리는 동기 등록 경로와 같은 단일 진입점을 써요 — 여기서 maybe_auto_scan만 직접 부르면
    # 배포 경로에 중복검토가 빠져요(ADR-017이 없앤 "경로별 누락"의 재발).
    outcome = run_post_registration(job.record_id, for_version=for_version)
    return outcome["scanned"]


def _seed_mcp_tool_drift(job) -> None:
    """배포·재배포로 등재된 MCP 의 도구 드리프트 원장을 맞춰요 (LC-03, 트리거 ②).

    재배포는 `record_id`를 유지하지만 도구 목록은 바뀔 수 있어요. 원장은 이름 기반이라
    통째로 덮이지 않고 대조돼요 — 관리자가 남긴 상태가 재배포로 사라지지 않아요(IH-22).
    등록 hook 처럼 이 경로는 HTTP 응답이 없으니 실패를 예외로 올리지 않아요.
    """
    if getattr(job, "asset_type", "") != "mcp":
        return
    record_id = getattr(job, "record_id", "")
    if not record_id:
        return
    try:
        from ...shared.deps import get_mcp_drift_service

        get_mcp_drift_service().seed_from_record(record_id)
    except Exception:
        _log.warning(
            "MCP 도구 드리프트 원장 seed에 실패했어요(배포 경로).",
            extra={"record_id": record_id}, exc_info=True,
        )


def _run_registration_hook(job) -> None:
    """배포 등재 경로의 등록 hook. HTTP 응답이 없는 경로라 반려도 예외로 올리지 않아요.

    여기서 예외를 던지면 이미 만들어진 AWS 리소스를 남기고 배포 폴링만 깨져요. 반려는
    hook 구현이 Registry에 REJECTED로 남기므로 감사와 검토 큐에서 그대로 보여요.
    """
    from ...shared.deps import (
        get_mcp_reregistration_cleanup,
        get_registration_gate,
    )
    from ...shared.governance import RegistrationRequest, RegistrationTrigger

    if (
        getattr(job, "asset_type", "") == "mcp"
        and not getattr(job, "is_redeploy", False)
    ):
        # 정상 재등록의 승인 회수는 exact old record를 가진 purge가 소유해요.
        # 여기서는 legacy/race Target 충돌을 unknown으로 관측만 합니다.
        try:
            report = get_mcp_reregistration_cleanup().reconcile(
                job.record_id,
                actor=getattr(job, "principal", "") or "system",
            )
            if report.unknown:
                _log.warning(
                    "MCP re-registration approval cleanup is unknown; "
                    "replacement_record_id=%s coordinates=%s",
                    job.record_id,
                    report.unknown,
                )
        except Exception:  # noqa: BLE001 - cleanup never blocks registration.
            _log.exception(
                "MCP re-registration approval cleanup failed; "
                "replacement_record_id=%s actor=%s",
                job.record_id,
                getattr(job, "principal", "") or "system",
            )

    try:
        get_registration_gate().on_registered(RegistrationRequest(
            record_id=job.record_id,
            principal=getattr(job, "principal", "") or "system",
            trigger=RegistrationTrigger.DEPLOY_REGISTER,
        ))
    except Exception:
        # 다음 poll/poller 순회가 같은 멱등 hook을 재시도해요.
        return


# Initializr 자동 승인의 근거 — 감사(statusReason)에 남겨요. 스캔 면제 표시는 스캔
# 기록을 만들지 않고 trust_adapter가 display-time으로 판정해요(scan read model 오염 방지).
_EXEMPT_REASON = "Initializr 자동 생성 agent — 플랫폼 표준 scaffold라 보안 스캔 면제·자동 승인"


def _approve_initializr_without_scan(record_id: str) -> bool:
    """Initializr 자산을 보안 스캔 없이 DRAFT→PENDING→APPROVED로 전이해요.

    스캔 면제는 **가짜 스캔 레코드로 남기지 않아요** — 그러면 dashboard/queue/inventory가
    이를 "스캔 완료·risk none"으로 오독해요(가짜 verdict). 대신 trust_adapter가
    Initializr-origin + APPROVED + 미스캔을 display-time으로 `스캔 면제`로 표시해요.
    """
    from ...shared.deps import get_registry, get_registry_id
    from ..catalog.registry.models import RecordStatus

    registry, registry_id = get_registry(), get_registry_id()
    try:
        current = registry.get_record(registry_id, record_id).status
        if current is RecordStatus.APPROVED:
            return False
        if current is RecordStatus.DRAFT:
            current = registry.submit_for_approval(registry_id, record_id)
        if current is RecordStatus.PENDING_APPROVAL:
            from ...shared.deps import get_asset_responsibility_port
            from ...shared.responsibility import ResponsibilityIncomplete

            try:
                get_asset_responsibility_port().require_for_approval(record_id)
            except ResponsibilityIncomplete:
                return False
            registry.update_status(
                registry_id, record_id, RecordStatus.APPROVED,
                reason=_EXEMPT_REASON,
            )
            return True
    except Exception:
        # 배포 READY 응답을 governance control-plane 일시 오류로 되돌리지 않아요.
        # 다음 poll/poller 순회가 같은 멱등 함수를 재시도합니다.
        return False
    return False


def _should_scan(latest, for_version: str) -> bool:
    """스캔을 돌려야 하는지 — 신규는 "기록 없음", 재배포는 "최신이 이 버전 아님" 기준."""
    if latest is None:
        return True
    if not for_version:
        return False                      # 신규 배포 poll 반복 — 기존 멱등 유지
    # 재배포: 최신 스캔이 이 버전을 이미 다뤘으면(완료·진행 중 모두) 건너뛰어요.
    # 버전 기록이 없는 옛 스캔은 "이 버전 아님"이라 재스캔 대상이 맞아요.
    return (getattr(latest, "version", "") or "") != for_version
