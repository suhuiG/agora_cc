"""MCP 도구 드리프트 관측과 민감도 변경 saga.

이 서비스가 하는 일은 네 가지예요.

1. **관측**: 등록된 MCP 자산의 업스트림 endpoint 에 `tools/list`를 다시 떠와요.
2. **대조**: 결과를 이름 기반 원장과 맞춰 상태를 갱신하고 `last_checked_at`을 남겨요.
3. **민감도 변경**: 방향별 승인 의도를 기록하고, IA-68 전에는 Target 이동을 막아요.
4. **재배포 준비**: 슬라이스 변경과 legacy Target 분할을 같은 saga로 게이트해요.

민감도 이동의 실제 Gateway 수정은 주입된 runtime movement port가 소유해요. 배포된 Lambda
Target 구성을 바꾸는 경로는 agent binding·Cedar·생성 코드 전파가 붙을 때까지 코드 소유
게이트가 닫혀 있어요. 연결형 catalog의 추가·변경·삭제·재등장은 Target 이름을 바꾸지 않는
명시 동기화를 거치고, 새 READY와 후속 상류 목록을 확인하기 전에는 태그 변경도 막아요.
두 번의 독립 관측에서 계속 부재한 MISSING만 RETIRED로 기록해요.

**관측 실패는 통과가 아니에요**(ADR-0037 §4). 도달 못 하면 원장의 도구 상태는 손대지
않고 `check_status=unknown` + 이유만 기록해요. 도달 실패를 MISSING 으로 번지게 하면
MCP 가 잠깐 죽은 것만으로 전 도구가 사라졌다고 원장이 거짓말해요.
"""
from __future__ import annotations

import copy
import json
import logging
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from ....shared.gateway_tools import (
    McpGatewayTargetError,
    mcp_gateway_target_index,
)
from ....shared.sensitivity_movement import (
    RedeploySensitivityDecision,
    SensitivityMovementRequest,
    SensitivityMovementResult,
    SensitivityPropagator,
)
from ..registry.models import DescriptorType, RecordNotFound, RegistryRecord
from .drift import confirm_sensitivity, reconcile, seed, shape_of
from .drift_models import (
    AssetDriftLedger,
    AssetToolDrift,
    DriftCheckStatus,
    McpTargetMode,
    SensitivityChangeEvent,
    SensitivityChangeRequest,
    SensitivityChangeStatus,
    SensitivitySource,
    ToolDriftState,
    ToolLedgerEntry,
)
from .drift_store import (
    DriftChangeConflict,
    DriftLedgerConflict,
)
from .protocol import McpProtocolError, McpToolInfo, fetch_mcp_tools
from .registry import McpEndpointError
from .sensitivity_admin import (
    ImpactCount,
    SensitivityChangeRejected,
    SensitivityChangePlan,
    is_downgrade,
    normalize_tag,
    plan_change,
    require_reason,
)

_log = logging.getLogger(__name__)
_APPLYING_RECOVERY_AFTER = timedelta(minutes=5)
_TARGET_PROPAGATION_TICKET = "IA-68"
_POST_SYNC_LEDGER_RETRIES = 3
_CONNECTED_SYNC_PENDING_REASON = (
    "Gateway Target 동기화 완료와 후속 도구 목록을 확인하고 있어요."
)
_CONNECTED_CATALOG_CHANGED_REASON = (
    "동기화 요청 뒤 상류 도구 목록이 다시 바뀌어 다시 동기화해야 해요."
)


class McpAssetNotFound(Exception):
    """드리프트 대상이 아닌 record_id(존재하지 않거나 MCP 자산이 아님)."""


class McpToolNotFound(Exception):
    """원장에 없는 도구 이름."""


class McpLedgerConflict(Exception):
    """관리자 확정 중 원장이 그새 바뀌었어요(낙관적 잠금 충돌, LC-05).

    폴러 등 다른 writer 가 admin 이 읽은 뒤 원장을 써서 버전이 어긋난 경우예요. 라우터가
    409 로 매핑해 "최신 상태를 다시 불러 확인"하도록 안내해요.
    """


class McpSensitivityChangeNotFound(Exception):
    """The requested sensitivity saga does not exist on this tool."""


class McpSensitivityChangeConflict(Exception):
    """Another request or ledger writer changed this tool first."""


class McpSensitivityPropagationFailed(Exception):
    """External movement failed and the durable request contains retry evidence."""

    def __init__(self, change: SensitivityChangeRequest) -> None:
        self.change = change
        super().__init__(change.error or "sensitivity propagation failed")


class McpSensitivityImpactChanged(SensitivityChangeRejected):
    """Approval impact changed and the durable request has the new snapshot."""

    def __init__(self, change: SensitivityChangeRequest) -> None:
        self.change = change
        super().__init__(
            "impact_changed",
            "영향받는 agent 목록이 요청 이후 달라져 다시 확인해야 해요.",
            remediation=(
                "최신 영향 목록을 확인한 뒤 승인을 다시 실행해 주세요."
            ),
        )


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def asset_key_of(record: RegistryRecord) -> str:
    """이름 기반 원장 키.

    Gateway target name 이 있으면 그걸 써요 — 라이브 도구 이름이 `{target}___{tool}`
    이라 target 이름은 불변이고, 재등록해도 같은 값이 나와요. 없으면 자산 이름으로
    폴백해요. **record_id 는 절대 쓰지 않아요**(IH-22: 재등록하면 원장이 고아가 돼요).
    """
    mcp = record.descriptors.get("mcp") if isinstance(record.descriptors, dict) else None
    if isinstance(mcp, dict):
        target = mcp.get("gatewayTargetName")
        if isinstance(target, str) and target.strip():
            return target.strip().lower()
    return (record.name or "").strip().lower()


def upstream_endpoint_of(record: RegistryRecord) -> str | None:
    """재조회에 쓸 업스트림 MCP endpoint.

    `endpoint`는 Gateway URL 로 덮여 있을 수 있어요(연결형 등록이 그래요). 그때 원본은
    `upstreamEndpoint`에 남아요. 업스트림을 모르면 None 이고, 그 자산은 `unknown`이에요 —
    Gateway URL 로는 인증 없이 `tools/list`를 못 떠오니까요.
    """
    mcp = record.descriptors.get("mcp") if isinstance(record.descriptors, dict) else None
    if not isinstance(mcp, dict):
        return None
    upstream = mcp.get("upstreamEndpoint")
    if isinstance(upstream, str) and upstream.strip():
        return upstream.strip()
    endpoint = mcp.get("endpoint")
    if isinstance(endpoint, str) and endpoint.strip() and not mcp.get("gatewayIdentifier"):
        # gateway 좌표가 없으면 endpoint 가 곧 업스트림이에요.
        return endpoint.strip()
    return None


def declared_tools(record: RegistryRecord) -> list[tuple[str, dict, str | None, str | None]]:
    """등록 시점 descriptor 에 박제된 (이름, {inputSchema·설명}, 민감도, 출처) 목록.

    출처는 `mcp.toolSensitivitySources` 에서 읽어요 — 등록 경로가 "MCP 가 선언했나 / 이름으로
    짐작했나 / 자동 분류가 채웠나"를 남기는 자리예요. 없으면 `None`(= 원장에서 `unknown`)
    이에요. 이 좌표는 Gateway target 에 넘기는 `tools.inlineContent` **밖에** 둬요 —
    Gateway 로 가는 페이로드를 이 티켓에서 건드리지 않기 위해서요.
    """
    mcp = record.descriptors.get("mcp") if isinstance(record.descriptors, dict) else None
    if not isinstance(mcp, dict):
        return []
    raw_sources = mcp.get("toolSensitivitySources")
    sources = raw_sources if isinstance(raw_sources, dict) else {}
    inline = (mcp.get("tools") or {}).get("inlineContent") if isinstance(
        mcp.get("tools"), dict) else None
    if not isinstance(inline, str) or not inline.strip():
        return []
    try:
        parsed = json.loads(inline)
    except (ValueError, TypeError):
        return []
    tools = parsed.get("tools") if isinstance(parsed, dict) else None
    out: list[tuple[str, dict, str | None, str | None]] = []
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        name = tool.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        out.append((
            name.strip(),
            {
                "inputSchema": tool.get("inputSchema"),
                "description": tool.get("description") or "",
            },
            tool.get("sensitivity") or None,
            # 도구 항목 안에 직접 적힌 값이 있으면 그걸 먼저 봐요(향후 등록 경로가 여기에
            # 넣을 수도 있어서요). 없으면 자산 단위 맵에서 읽어요.
            tool.get("sensitivitySource") or sources.get(name.strip()) or None,
        ))
    return out


class McpDriftService:
    def __init__(self, *, registry, registry_id: str, store,
                 fetch=None, now=None, max_records: int | None = 1000,
                 target_name_resolver=None,
                 propagate: SensitivityPropagator | None = None,
                 target_propagation_enabled: bool = False,
                 synchronize_connected_target: (
                     Callable[[str, str], dict] | None
                 ) = None) -> None:
        self._registry = registry
        self._registry_id = registry_id
        self._store = store
        self._fetch = fetch or fetch_mcp_tools
        self._now = now or _utc_now
        self._max_records = max_records
        self._target_name_resolver = (
            target_name_resolver or self._default_target_names
        )
        self._propagate = propagate
        self._target_propagation_enabled = target_propagation_enabled
        self._synchronize_connected_target = synchronize_connected_target

    @staticmethod
    def _default_target_names(asset_name: str) -> dict[str, str]:
        """Test-compatible fallback; production injects the deploy slice accessor."""
        from ....shared.slug import gateway_target_name
        from ..registry.models import SensitivityTag

        return {
            tag.value: gateway_target_name(asset_name, sensitivity=tag.value)
            for tag in SensitivityTag
        }

    # --- 열거 ---------------------------------------------------------------

    def _mcp_records(self) -> list[RegistryRecord]:
        records = self._registry.list_records(
            self._registry_id, max_results=self._max_records)
        return [r for r in records if r.descriptor_type is DescriptorType.MCP]

    def _record(self, record_id: str) -> RegistryRecord:
        try:
            record = self._registry.get_record(self._registry_id, record_id)
        except RecordNotFound as e:
            raise McpAssetNotFound(record_id) from e
        if record.descriptor_type is not DescriptorType.MCP:
            raise McpAssetNotFound(record_id)
        return record

    # --- 읽기(네트워크 없음) -----------------------------------------------

    def snapshot(self) -> list[AssetToolDrift]:
        """원장만 읽어 전 MCP 자산의 드리프트 상태를 돌려줘요.

        원장이 비어 있는 자산은 등록 descriptor 로 seed 해요(한 번만). 이렇게 안 하면
        기존 자산 전부가 "도구 0개"로 보여서, 화면이 드리프트 없음처럼 읽혀요.
        """
        out: list[AssetToolDrift] = []
        for record in self._mcp_records():
            ledger = self._store.get(asset_key_of(record))
            if not ledger.entries:
                ledger = self._seed(record)
            out.append(self._to_snapshot(record, ledger))
        return sorted(out, key=lambda s: s.asset_name)

    def snapshot_for(self, record_id: str) -> AssetToolDrift:
        record = self._record(record_id)
        ledger = self._store.get(asset_key_of(record))
        if not ledger.entries:
            ledger = self._seed(record)
        return self._to_snapshot(record, ledger)

    # --- seed(등록·재등록 경로) --------------------------------------------

    def seed_from_record(self, record_id: str) -> AssetToolDrift:
        """등록·재배포 직후 훅. 네트워크 없이 descriptor 를 원장에 반영해요."""
        record = self._record(record_id)
        return self._to_snapshot(record, self._seed(record))

    def _seed(self, record: RegistryRecord) -> AssetDriftLedger:
        asset_key = asset_key_of(record)
        existing = self._store.get(asset_key)
        declared = declared_tools(record)
        if not declared:
            return existing
        now = self._now()
        if existing.entries:
            # 이미 원장이 있어요(재등록). descriptor 를 관측으로 취급해 **대조**해요 —
            # 통째로 덮어쓰면 관리자가 남긴 상태가 사라져요(그게 IH-22 의 증상이에요).
            entries = reconcile(
                previous=existing.entries,
                observed=[
                    (name, shape_of(meta["description"], meta["inputSchema"]))
                    for name, meta, _tag, _source in declared
                ],
                now=now,
            )
            if self._is_deployed(record):
                entries = self._preserve_deployed_missing_membership(
                    record,
                    previous=existing.entries,
                    reconciled=entries,
                    declared_sensitivities={
                        name: tag
                        for name, _meta, tag, _source in declared
                    },
                )
            reconciled = AssetDriftLedger(
                asset_key=asset_key,
                check_status=existing.check_status,
                last_checked_at=existing.last_checked_at,
                check_error=existing.check_error,
                catalog_sync_pending=existing.catalog_sync_pending,
                catalog_snapshot_unobservable=(
                    existing.catalog_snapshot_unobservable
                ),
                entries=entries,
                version=existing.version,
            )
            try:
                new_version = self._store.put(reconciled)
            except DriftLedgerConflict:
                # 동시 seed/write 가 먼저 반영됐어요 — 남의 결과를 채택해요. 읽기 경로
                # (snapshot/preview)가 seed 를 트리거하므로 여기서 409 가 튀면 안 돼요.
                return self._store.get(asset_key)
            return replace(reconciled, version=new_version)
        connected_unobservable = (
            not self._is_deployed(record)
            and existing.check_status is DriftCheckStatus.UNKNOWN
        )
        ledger = AssetDriftLedger(
            asset_key=asset_key,
            check_status=(
                DriftCheckStatus.UNKNOWN
                if connected_unobservable
                else DriftCheckStatus.NEVER_CHECKED
            ),
            last_checked_at=(
                existing.last_checked_at
                if connected_unobservable
                else None
            ),
            check_error=(
                existing.check_error
                if connected_unobservable
                else None
            ),
            catalog_sync_pending=connected_unobservable,
            catalog_snapshot_unobservable=connected_unobservable,
            entries=seed(
                tools=[
                    (name, shape_of(meta["description"], meta["inputSchema"]), tag)
                    for name, meta, tag, _source in declared
                ],
                sources={name: source for name, _meta, _tag, source in declared},
                now=now,
            ),
            version=existing.version,
        )
        try:
            new_version = self._store.put(ledger)
        except DriftLedgerConflict:
            # 동시 최초 seed 경합 — 남의 seed 를 채택해요(re-read).
            return self._store.get(asset_key)
        return replace(ledger, version=new_version)

    @staticmethod
    def _preserve_deployed_missing_membership(
        record: RegistryRecord,
        *,
        previous: tuple[ToolLedgerEntry, ...],
        reconciled: tuple[ToolLedgerEntry, ...],
        declared_sensitivities: dict[str, str | None],
    ) -> tuple[ToolLedgerEntry, ...]:
        """Do not mistake an unchanged deployed descriptor for upstream evidence."""
        try:
            target_index = mcp_gateway_target_index(record.descriptors)
        except McpGatewayTargetError:
            return reconciled
        if not target_index.split:
            return reconciled
        memberships = {
            operation: target.sensitivity
            for target in target_index.targets
            for operation in target.operations
        }
        previous_by_name = {entry.tool_name: entry for entry in previous}
        return tuple(
            prior
            if (
                (prior := previous_by_name.get(entry.tool_name)) is not None
                and prior.state is ToolDriftState.MISSING
                and prior.previous_sensitivity
                and memberships.get(entry.tool_name) == prior.previous_sensitivity
                and declared_sensitivities.get(entry.tool_name)
                == prior.previous_sensitivity
            )
            else entry
            for entry in reconciled
        )

    # --- 관측(네트워크) ----------------------------------------------------

    def resync(self, record_id: str) -> AssetToolDrift:
        """관리자 "다시 읽기" — `tools/list`를 실제로 떠와 원장과 대조해요."""
        record = self._record(record_id)
        return self._to_snapshot(record, self._resync(record))

    def resync_all(self) -> list[AssetToolDrift]:
        """주기 폴러용. 한 자산이 실패해도 나머지는 계속 봐요."""
        out: list[AssetToolDrift] = []
        for record in self._mcp_records():
            try:
                out.append(self._to_snapshot(record, self._resync(record)))
            except Exception:  # 스토어 장애 등 — 다음 주기에 재시도
                _log.exception("mcp drift resync failed: record_id=%s", record.record_id)
        return out

    def _resync(self, record: RegistryRecord) -> AssetDriftLedger:
        asset_key = asset_key_of(record)
        previous = self._store.get(asset_key)
        if not previous.entries:
            previous = self._seed(record)
        now = self._now()
        endpoint = upstream_endpoint_of(record)
        if not endpoint:
            return self._record_unobserved(
                previous, now,
                "업스트림 MCP endpoint를 알 수 없어요 — Gateway URL로는 tools/list를 "
                "인증 없이 조회할 수 없어요.",
            )
        try:
            info = self._fetch(endpoint)
        except (McpProtocolError, McpEndpointError) as e:
            return self._record_unobserved(previous, now, f"MCP 조회 실패: {e}")
        except Exception as e:                      # 타임아웃·DNS·TLS 등
            return self._record_unobserved(previous, now, f"MCP 조회 실패: {e}")

        entries = reconcile(
            previous=previous.entries,
            observed=[
                (tool.name, shape_of(tool.description, tool.input_schema))
                for tool in info.tools
            ],
            now=now,
        )
        connected_sync_required = (
            not self._is_deployed(record)
            and (
                previous.catalog_sync_pending
                or self._catalog_signature(previous.entries)
                != self._catalog_signature(entries)
            )
        )
        ledger = AssetDriftLedger(
            asset_key=asset_key,
            check_status=(
                DriftCheckStatus.UNKNOWN
                if connected_sync_required
                else DriftCheckStatus.OK
            ),
            last_checked_at=now,
            check_error=(
                _CONNECTED_SYNC_PENDING_REASON
                if connected_sync_required
                else None
            ),
            catalog_sync_pending=connected_sync_required,
            catalog_snapshot_unobservable=(
                previous.catalog_snapshot_unobservable
            ),
            entries=entries,
            version=previous.version,
        )
        # 폴러 경로예요. CAS 가 conflict 를 내면 그게 곧 "admin 편집을 안 덮어씀"이에요 —
        # resync_all 이 예외를 삼키고 다음 주기에 최신을 다시 읽어요.
        new_version = self._store.put(ledger)
        persisted = replace(ledger, version=new_version)
        if connected_sync_required:
            return self._synchronize_connected_catalog(record, persisted)
        return persisted

    @staticmethod
    def _catalog_signature(entries: tuple[ToolLedgerEntry, ...]) -> dict:
        """Return the upstream-owned catalog shape, excluding Agora tag state."""
        return {
            entry.tool_name: entry.observed.to_dict()
            for entry in entries
            if entry.state not in {
                ToolDriftState.MISSING,
                ToolDriftState.RETIRED,
            }
        }

    def _synchronize_connected_catalog(
        self,
        record: RegistryRecord,
        ledger: AssetDriftLedger,
    ) -> AssetDriftLedger:
        """Synchronize a changed connected catalog and verify the resulting view.

        A connected Target owns no inline tool list: AgentCore discovers the upstream
        catalog and keeps that snapshot until create/update or an explicit synchronize.
        Synchronizing does not rename the Target, so it does not need IA-68's binding,
        Cedar, and generated-code propagation. We still distrust the 202 response:
        the adapter must observe a newer READY GetGatewayTarget result, and this service
        probes that observed endpoint again before recording RETIRED.

        Deployed Lambda Targets are intentionally excluded. Agora owns their inline
        schema, so removal belongs to the already-gated redeploy path in IA-68. A
        deployed MISSING entry remains visible with unknown callability meanwhile.
        """
        if self._is_deployed(record):
            # Lambda tool schemas are Agora-owned and can only be changed by the
            # IA-68 redeploy path. Connected Targets own only an endpoint, so their
            # catalog can be synchronized without renaming the Target or rewriting
            # binding/Cedar/generated-code coordinates.
            return ledger
        missing_names = {
            entry.tool_name
            for entry in ledger.entries
            if entry.state is ToolDriftState.MISSING
        }
        requested_catalog_signature = self._catalog_signature(ledger.entries)
        if self._synchronize_connected_target is None:
            return self._persist_connected_sync_unknown(
                ledger,
                "연결형 Gateway Target 동기화 경로가 연결되지 않았어요.",
            )
        mcp = self._mcp_node(record)
        gateway_id = str(mcp.get("gatewayIdentifier") or "").strip()
        target_id = str(mcp.get("gatewayTargetId") or "").strip()
        target_name = str(mcp.get("gatewayTargetName") or "").strip()
        expected_endpoint = upstream_endpoint_of(record) or ""
        if (
            not gateway_id
            or not target_id
            or not target_name
            or not expected_endpoint
        ):
            return self._persist_connected_sync_unknown(
                ledger,
                "연결형 Gateway Target 동기화 좌표를 확인하지 못했어요.",
            )

        try:
            synchronization = self._synchronize_connected_target(
                gateway_id,
                target_id,
                expected_target_name=target_name,
                expected_endpoint=expected_endpoint,
            )
        except Exception:
            _log.exception(
                "connected MCP target synchronization failed: "
                "record_id=%s target_id=%s",
                record.record_id,
                target_id,
            )
            return self._persist_connected_sync_unknown(
                ledger,
                "Gateway Target 동기화 호출이 실패했어요.",
            )
        if (
            not isinstance(synchronization, dict)
            or str(synchronization.get("status") or "").lower() != "ready"
        ):
            reason = (
                str(synchronization.get("reason") or "")
                if isinstance(synchronization, dict)
                else ""
            )
            return self._persist_connected_sync_unknown(
                ledger,
                "Gateway Target 동기화 완료를 확인하지 못했어요"
                + (f": {reason}" if reason else "."),
            )

        observed_endpoint = str(
            synchronization.get("endpoint") or ""
        ).strip()
        if not observed_endpoint:
            return self._persist_connected_sync_unknown(
                ledger,
                "동기화 완료 관측에서 MCP endpoint를 확인하지 못했어요.",
            )
        try:
            observed = self._fetch(observed_endpoint)
        except (McpProtocolError, McpEndpointError):
            _log.exception(
                "connected MCP post-synchronization probe failed: "
                "record_id=%s target_id=%s",
                record.record_id,
                target_id,
            )
            return self._persist_connected_sync_unknown(
                ledger,
                "동기화 후속 MCP 도구 목록을 확인하지 못했어요.",
            )
        except Exception:
            _log.exception(
                "connected MCP post-synchronization probe failed: "
                "record_id=%s target_id=%s",
                record.record_id,
                target_id,
            )
            return self._persist_connected_sync_unknown(
                ledger,
                "동기화 후속 MCP 도구 목록을 확인하지 못했어요.",
            )

        observed_at = self._now()
        base = ledger
        for attempt in range(_POST_SYNC_LEDGER_RETRIES):
            if (
                attempt > 0
                and
                base.last_checked_at
                and base.last_checked_at >= observed_at
            ):
                # A conflict means another observer published after this probe.
                # Second-resolution timestamps can be equal, so equality is
                # newer-or-concurrent rather than permission to replay stale data.
                return base
            post_sync = self._connected_post_sync_ledger(
                base,
                observed=observed,
                synchronized_missing_names=missing_names,
                requested_catalog_signature=requested_catalog_signature,
                observed_at=observed_at,
            )
            try:
                new_version = self._store.put(post_sync)
            except DriftLedgerConflict:
                base = self._store.get(ledger.asset_key)
                continue
            return replace(post_sync, version=new_version)
        _log.error(
            "connected MCP post-synchronization ledger remained contended: "
            "record_id=%s target_id=%s",
            record.record_id,
            target_id,
        )
        return self._persist_connected_sync_unknown(
            base,
            "동기화 후 MCP 관측을 동시 원장 변경 때문에 저장하지 못했어요.",
        )

    def _persist_connected_sync_unknown(
        self,
        ledger: AssetDriftLedger,
        reason: str,
    ) -> AssetDriftLedger:
        observed = ledger
        base = ledger
        for _attempt in range(_POST_SYNC_LEDGER_RETRIES):
            unknown = replace(
                base,
                check_status=DriftCheckStatus.UNKNOWN,
                check_error=reason,
                catalog_sync_pending=True,
            )
            try:
                new_version = self._store.put(unknown)
            except DriftLedgerConflict:
                base = self._store.get(ledger.asset_key)
                if self._has_newer_connected_observation(observed, base):
                    return base
                continue
            return replace(unknown, version=new_version)
        _log.error(
            "connected MCP synchronization status remained contended: asset=%s",
            ledger.asset_key,
        )
        # Do not return an in-memory UNKNOWN that the durable ledger does not
        # contain. In the pre-sync call site this also prevents an external
        # synchronization write without a persisted in-progress marker.
        raise DriftLedgerConflict(ledger.asset_key)

    @staticmethod
    def _has_newer_connected_observation(
        observed: AssetDriftLedger,
        current: AssetDriftLedger,
    ) -> bool:
        if observed.catalog_sync_pending and not current.catalog_sync_pending:
            return True
        if (
            current.last_checked_at
            and (
                not observed.last_checked_at
                or current.last_checked_at > observed.last_checked_at
            )
        ):
            return True
        current_by_name = {
            entry.tool_name: entry
            for entry in current.entries
        }
        return any(
            (
                current_entry := current_by_name.get(entry.tool_name)
            ) is not None
            and current_entry.state is not entry.state
            for entry in observed.entries
            if entry.state is ToolDriftState.MISSING
        )

    @staticmethod
    def _connected_post_sync_ledger(
        base: AssetDriftLedger,
        *,
        observed,
        synchronized_missing_names: set[str],
        requested_catalog_signature: dict,
        observed_at: str,
    ) -> AssetDriftLedger:
        observed_names = {tool.name for tool in observed.tools}
        reconciled_entries = reconcile(
            previous=base.entries,
            observed=[
                (tool.name, shape_of(tool.description, tool.input_schema))
                for tool in observed.tools
            ],
            now=observed_at,
        )
        candidate_missing_names = {
            entry.tool_name
            for entry in base.entries
            if (
                entry.tool_name in synchronized_missing_names
                and entry.state is ToolDriftState.MISSING
            )
        }
        retired_names = candidate_missing_names - observed_names
        post_sync_entries = tuple(
            entry.with_state(ToolDriftState.RETIRED, now=observed_at)
            if entry.tool_name in retired_names
            else entry
            for entry in reconciled_entries
        )
        catalog_changed_after_request = (
            McpDriftService._catalog_signature(reconciled_entries)
            != requested_catalog_signature
        )
        return replace(
            base,
            check_status=(
                DriftCheckStatus.UNKNOWN
                if catalog_changed_after_request
                else DriftCheckStatus.OK
            ),
            check_error=(
                _CONNECTED_CATALOG_CHANGED_REASON
                if catalog_changed_after_request
                else None
            ),
            last_checked_at=observed_at,
            catalog_sync_pending=catalog_changed_after_request,
            catalog_snapshot_unobservable=(
                base.catalog_snapshot_unobservable
                and catalog_changed_after_request
            ),
            entries=post_sync_entries,
        )

    def _record_unobserved(self, previous: AssetDriftLedger, now: str,
                           reason: str) -> AssetDriftLedger:
        """관측 실패 — 도구 상태는 그대로 두고 실패 사실만 남겨요.

        `last_checked_at`도 갱신하지 않아요. 갱신하면 화면이 "방금 확인함"으로 읽히는데
        실제로 확인한 건 없어요.
        """
        ledger = AssetDriftLedger(
            asset_key=previous.asset_key,
            check_status=DriftCheckStatus.UNKNOWN,
            last_checked_at=previous.last_checked_at,
            check_error=reason,
            catalog_sync_pending=previous.catalog_sync_pending,
            catalog_snapshot_unobservable=(
                previous.catalog_snapshot_unobservable
            ),
            entries=previous.entries,
            version=previous.version,
        )
        new_version = self._store.put(ledger)
        return replace(ledger, version=new_version)

    # --- 관리자 민감도 확정 (티켓 A) -----------------------------------------

    def _entry(self, record: RegistryRecord, tool_name: str
               ) -> tuple[AssetDriftLedger, ToolLedgerEntry]:
        asset_key = asset_key_of(record)
        self._discard_obsolete_missing_changes(asset_key)
        ledger = self._store.get(asset_key)
        if not ledger.entries:
            ledger = self._seed(record)
        for entry in ledger.entries:
            if entry.tool_name == tool_name:
                return ledger, entry
        raise McpToolNotFound(tool_name)

    def plan_sensitivity(self, record_id: str, tool_name: str,
                         sensitivity: object) -> SensitivityChangePlan:
        """바꾸기 **전에** 무엇이 달라지는지 계산해요. 원장을 쓰지 않아요."""
        record = self._record(record_id)
        self._require_movable_target_ledger(record)
        ledger, entry = self._entry(record, tool_name)
        tag = normalize_tag(sensitivity)
        get_change = getattr(self._store, "get_change", None)
        active_change = (
            get_change(ledger.asset_key or asset_key_of(record), tool_name)
            if get_change is not None
            else None
        )
        impact_target = (
            active_change.target_before
            if (
                active_change is not None
                and active_change.status
                is not SensitivityChangeStatus.APPLIED
                and active_change.before == entry.sensitivity
                and active_change.after == tag
            )
            else None
        )
        target_before = impact_target or self._expected_target(
            record,
            entry.sensitivity,
        )
        target_after = self._expected_target(record, tag)
        plan = plan_change(
            entry,
            tag,
            impact=self._impact(
                record,
                tool_name,
                gateway_target_names=self._impact_target_names(
                    record,
                    before=entry.sensitivity,
                    after=tag,
                    target_before=target_before,
                    target_after=target_after,
                ),
            ),
        )
        self._require_connected_catalog_ready(record, ledger)
        return plan

    def set_sensitivity(
        self,
        record_id: str,
        tool_name: str,
        sensitivity: object,
        *,
        reason: str,
        actor: str,
        actor_label: str = "",
    ) -> tuple[
        AssetToolDrift,
        SensitivityChangePlan,
        SensitivityChangeRequest,
    ]:
        """Record upgrades as approved waits and queue downgrades for approval."""
        record = self._record(record_id)
        self._require_movable_target_ledger(record)
        ledger, entry = self._entry(record, tool_name)
        tag = normalize_tag(sensitivity)
        target_before = self._expected_target(record, entry.sensitivity)
        target_after = self._expected_target(record, tag)
        plan = plan_change(
            entry,
            tag,
            impact=self._impact(
                record,
                tool_name,
                gateway_target_names=self._impact_target_names(
                    record,
                    before=entry.sensitivity,
                    after=tag,
                    target_before=target_before,
                    target_after=target_after,
                ),
            ),
        )
        self._require_connected_catalog_ready(record, ledger)
        checked_reason = require_reason(plan, reason)
        if plan.no_op:
            raise SensitivityChangeRejected(
                "no_change",
                "현재 민감도와 상태가 이미 같아요.",
                remediation="다른 민감도를 선택해 주세요.",
            )
        connected_tool_definition = (
            self._prepare_connected_tool_definition(
                record,
                ledger,
                entry,
            )
        )
        now = self._now()
        asset_key = ledger.asset_key or asset_key_of(record)
        change = SensitivityChangeRequest(
            request_id=uuid4().hex,
            asset_key=asset_key,
            record_id=record.record_id,
            tool_name=tool_name,
            before=plan.before,
            after=plan.after,
            before_source=plan.before_source,
            state_before=plan.state_before.value,
            state_after=plan.state_after.value,
            direction="downgrade" if plan.downgrade else "upgrade",
            status=SensitivityChangeStatus.PENDING_APPROVAL,
            reason=checked_reason,
            requested_by=actor or "unknown",
            requested_by_label=actor_label or "",
            requested_at=now,
            ledger_version=ledger.version,
            groups_gained=plan.groups_gained,
            groups_lost=plan.groups_lost,
            impact=tuple(item.to_dict() for item in plan.impact),
            target_before=target_before,
            target_after=target_after,
        )
        try:
            change = self._store.create_change(
                change,
                self._change_event(
                    change,
                    stage="requested",
                    actor=actor,
                    actor_label=actor_label,
                    at=now,
                ),
            )
        except DriftChangeConflict as exc:
            raise McpSensitivityChangeConflict(tool_name) from exc

        if plan.downgrade:
            return self._to_snapshot(record, ledger), plan, change

        if not self._agent_impact_known(plan):
            failed = self._fail_before_apply(
                change,
                error=self._agent_impact_reason(plan),
                actor=actor,
                actor_label=actor_label,
            )
            raise McpSensitivityPropagationFailed(failed)
        return self._apply_change(
            record,
            ledger,
            entry,
            plan,
            change,
            actor=actor,
            actor_label=actor_label,
            approval_stage="approved",
            expected_status=SensitivityChangeStatus.PENDING_APPROVAL,
            connected_tool_definition=connected_tool_definition,
        )

    def prepare_redeploy(
        self,
        record_id: str,
        tools_inline: str,
        *,
        actor: str,
        actor_label: str = "",
    ) -> RedeploySensitivityDecision:
        """Apply Target membership prerequisites before an MCP redeploy."""
        record = self._record(record_id)
        self._discard_obsolete_missing_changes(asset_key_of(record))
        mcp = self._mcp_node(record)
        if (
            not isinstance(mcp.get("gatewayTargets"), list)
            and mcp.get("gatewayTargetName")
            and self._is_deployed(record)
        ):
            ledger = self._store.get(asset_key_of(record))
            if not ledger.entries:
                ledger = self._seed(record)
            if not ledger.entries:
                return {
                    "status": "failed",
                    "reason": "legacy Target의 도구 원장을 확인하지 못했어요.",
                }
            ledger_error = self._sensitivity_ledger_error(record, ledger)
            if ledger_error:
                return {"status": "failed", "reason": ledger_error}
            entry = ledger.entries[0]
            plan = plan_change(
                entry,
                entry.sensitivity,
                impact=self._legacy_impact(record, ledger.entries),
            )
            now = self._now()
            change = SensitivityChangeRequest(
                request_id=uuid4().hex,
                asset_key=ledger.asset_key or asset_key_of(record),
                record_id=record.record_id,
                tool_name=entry.tool_name,
                before=entry.sensitivity,
                after=entry.sensitivity,
                before_source=entry.sensitivity_source,
                state_before=entry.state.value,
                state_after=entry.state.value,
                direction="upgrade",
                status=SensitivityChangeStatus.PENDING_APPROVAL,
                reason="재배포 전 legacy Target을 민감도별로 분할해요.",
                requested_by=actor or "unknown",
                requested_by_label=actor_label or "",
                requested_at=now,
                ledger_version=ledger.version,
                impact=tuple(item.to_dict() for item in plan.impact),
                target_before=str(mcp.get("gatewayTargetName") or ""),
                target_after=self._expected_target(
                    record,
                    entry.sensitivity,
                ),
            )
            try:
                change = self._store.create_change(
                    change,
                    self._change_event(
                        change,
                        stage="requested",
                        actor=actor,
                        actor_label=actor_label,
                        at=now,
                    ),
                )
            except DriftChangeConflict:
                existing = self._store.get_change(
                    change.asset_key,
                    change.tool_name,
                )
                if (
                    existing is not None
                    and existing.status is SensitivityChangeStatus.APPLIED
                ):
                    return self.prepare_redeploy(
                        record_id,
                        tools_inline,
                        actor=actor,
                        actor_label=actor_label,
                    )
                if (
                    existing is not None
                    and self._redeploy_change_retryable(existing)
                ):
                    try:
                        _snapshot, _plan, resumed = (
                            self.retry_sensitivity_change(
                                record_id,
                                entry.tool_name,
                                existing.request_id,
                                actor=actor,
                                actor_label=actor_label,
                            )
                        )
                    except McpSensitivityPropagationFailed as exc:
                        return {
                            "status": "failed",
                            "change": exc.change.to_dict(),
                        }
                    if resumed.status is SensitivityChangeStatus.APPLIED:
                        return self.prepare_redeploy(
                            record_id,
                            tools_inline,
                            actor=actor,
                            actor_label=actor_label,
                        )
                    return self._pending_redeploy_decision(resumed)
                return self._pending_redeploy_decision(existing)
            return {
                "status": "pending_approval",
                "change": change.to_dict(),
            }

        try:
            incoming_sensitivities = self._redeploy_sensitivities(tools_inline)
        except (SensitivityChangeRejected, TypeError, ValueError) as exc:
            return {
                "status": "failed",
                "reason": str(exc),
            }

        ledger = self._store.get(asset_key_of(record))
        if not ledger.entries:
            ledger = self._seed(record)
        ledger_error = self._sensitivity_ledger_error(record, ledger)
        if ledger_error:
            return {"status": "failed", "reason": ledger_error}
        target_error = self._target_ledger_error(record)
        if target_error:
            return {"status": "failed", "reason": target_error}
        applied_changes: list[dict] = []
        declared = declared_tools(record)
        declared_names = {
            name for name, _meta, _sensitivity, _source in declared
        }
        missing_entries = sorted(
            (
                entry
                for entry in ledger.entries
                if entry.state is ToolDriftState.MISSING
            ),
            key=lambda entry: entry.tool_name,
        )
        target_index = mcp_gateway_target_index(record.descriptors)
        missing_target_sensitivities = {
            entry.tool_name: next(
                (
                    target.sensitivity
                    for target in target_index.targets
                    if entry.tool_name in target.operations
                ),
                None,
            )
            for entry in missing_entries
        }
        missing_target_changes = [
            entry
            for entry in missing_entries
            if (
                (
                    missing_target_sensitivities[entry.tool_name] is not None
                    and incoming_sensitivities.get(entry.tool_name)
                    != missing_target_sensitivities[entry.tool_name]
                )
                or (
                    missing_target_sensitivities[entry.tool_name] is None
                    and entry.tool_name in declared_names
                    and incoming_sensitivities.get(entry.tool_name) is not None
                )
            )
        ]
        if missing_target_changes:
            details = ", ".join(
                (
                    f"{entry.tool_name} (현재 Target membership "
                    f"{missing_target_sensitivities[entry.tool_name] or '없음'}, "
                    f"직전 참고값 {entry.previous_sensitivity or '미분류'}, "
                    f"{target_index.target_name(entry.tool_name) or 'Target 확인 불가'})"
                )
                for entry in missing_target_changes
            )
            return {
                "status": "pending_propagation",
                "reason": (
                    "배포형 MISSING 도구의 live Target 제거는 실행하지 않아요: "
                    f"{details}. 호출 가능성은 확인되지 않았고, per-agent 정책이 남은 "
                    "Gateway에서는 정책 갱신이 막힐 수 있어요. IA-53 컷오버 상태를 "
                    "확인한 뒤 agent binding, Cedar 정책, 생성 코드를 함께 전파하는 "
                    f"{_TARGET_PROPAGATION_TICKET}에서 제거해요."
                ),
            }
        classified_names = {
            entry.tool_name
            for entry in ledger.entries
            if entry.sensitivity
            and entry.state not in {
                ToolDriftState.MISSING,
                ToolDriftState.RETIRED,
            }
        }
        # A MISSING tool may be redeployed unchanged in its existing Target. This
        # does not restore its ledger classification or move live membership; it
        # only avoids turning an unrelated code redeploy into an IA-68 removal.
        classified_names.update(
            entry.tool_name
            for entry in missing_entries
            if missing_target_sensitivities[entry.tool_name] is not None
        )
        sanitized_tools_inline = self._sanitize_redeploy_tools(
            tools_inline,
            classified_names=classified_names,
        )
        desired = self._redeploy_sensitivities(sanitized_tools_inline)
        changed_entries = [
            (entry, desired.get(entry.tool_name))
            for entry in ledger.entries
            if (
                entry.tool_name in declared_names
                and entry.state not in {
                    ToolDriftState.MISSING,
                    ToolDriftState.RETIRED,
                }
                and desired.get(entry.tool_name) != entry.sensitivity
            )
        ]
        changed_entries.sort(
            key=lambda item: (
                is_downgrade(item[0].sensitivity, item[1]),
                item[0].tool_name,
            )
        )
        for entry, after in changed_entries:

            existing = self._store.get_change(
                ledger.asset_key,
                entry.tool_name,
            )
            if (
                existing is not None
                and existing.status is not SensitivityChangeStatus.APPLIED
            ):
                if (
                    existing.before != entry.sensitivity
                    or existing.after != after
                ):
                    return {
                        "status": "blocked",
                        "reason": "다른 민감도 변경 요청이 진행 중이에요.",
                        "change": existing.to_dict(),
                    }
                if self._redeploy_change_retryable(existing):
                    try:
                        _snapshot, _plan, resumed = (
                            self.retry_sensitivity_change(
                                record_id,
                                entry.tool_name,
                                existing.request_id,
                                actor=actor,
                                actor_label=actor_label,
                            )
                        )
                    except McpSensitivityPropagationFailed as exc:
                        return {
                            "status": "failed",
                            "change": exc.change.to_dict(),
                        }
                    if resumed.status is not SensitivityChangeStatus.APPLIED:
                        return self._pending_redeploy_decision(resumed)
                    applied_changes.append(resumed.to_dict())
                    continue
                return self._pending_redeploy_decision(existing)

            try:
                _snapshot, _plan, change = self.set_sensitivity(
                    record_id,
                    entry.tool_name,
                    after,
                    reason=(
                        "재배포 산출물의 민감도 변경을 반영해요."
                    ),
                    actor=actor,
                    actor_label=actor_label,
                )
            except McpSensitivityPropagationFailed as exc:
                return {"status": "failed", "change": exc.change.to_dict()}
            except SensitivityChangeRejected as exc:
                return {
                    "status": "blocked",
                    "reason": f"{exc.message} {exc.remediation}".strip(),
                    "impact": [
                        item.to_dict()
                        for item in self._impact(
                            record,
                            entry.tool_name,
                            gateway_target_names=self._impact_target_names(
                                record,
                                before=entry.sensitivity,
                                after=after,
                            ),
                        )
                    ],
                }
            except McpSensitivityChangeConflict:
                return {
                    "status": "blocked",
                    "reason": "민감도 변경 요청이 동시에 갱신됐어요.",
                }
            if change.status is not SensitivityChangeStatus.APPLIED:
                return self._pending_redeploy_decision(change)
            applied_changes.append(change.to_dict())

        refreshed = self._record(record_id)
        target_error = self._target_ledger_error(refreshed)
        if target_error:
            return {"status": "failed", "reason": target_error}
        return {
            "status": "ready",
            "changes": applied_changes,
            "tools_inline": sanitized_tools_inline,
        }

    @staticmethod
    def _redeploy_sensitivities(tools_inline: str) -> dict[str, str | None]:
        document = json.loads(tools_inline)
        tools = document.get("tools") if isinstance(document, dict) else None
        if not isinstance(tools, list):
            raise ValueError("재배포 tool schema 형식이 올바르지 않아요.")
        sensitivities: dict[str, str | None] = {}
        for tool in tools:
            if not isinstance(tool, dict):
                raise ValueError("재배포 tool 항목 형식이 올바르지 않아요.")
            name = str(tool.get("name") or "").strip()
            if not name:
                raise ValueError("재배포 tool 이름이 없어요.")
            if name in sensitivities:
                raise ValueError(f"재배포 tool 이름이 중복됐어요: {name}")
            sensitivities[name] = normalize_tag(tool.get("sensitivity"))
        return sensitivities

    @staticmethod
    def _sanitize_redeploy_tools(
        tools_inline: str,
        *,
        classified_names: set[str],
    ) -> str:
        """Strip owner classifications until the current descriptor is classified."""
        document = json.loads(tools_inline)
        tools = document.get("tools") if isinstance(document, dict) else None
        if not isinstance(tools, list):
            raise ValueError("재배포 tool schema 형식이 올바르지 않아요.")
        for tool in tools:
            if not isinstance(tool, dict):
                raise ValueError("재배포 tool 항목 형식이 올바르지 않아요.")
            name = str(tool.get("name") or "").strip()
            if name in classified_names:
                continue
            tool.pop("sensitivity", None)
            tool.pop("sensitivitySource", None)
        return json.dumps(
            document,
            ensure_ascii=False,
            separators=(",", ":"),
        )

    def _target_ledger_error(self, record: RegistryRecord) -> str:
        mcp = McpDriftService._mcp_node(record)
        raw_targets = mcp.get("gatewayTargets")
        if not isinstance(raw_targets, list):
            return "재배포 전 Gateway Target 원장을 확인하지 못했어요."
        memberships: dict[str, str] = {}
        expected_names = self._target_name_resolver(record.name)
        seen_sensitivities: set[str] = set()
        for target in raw_targets:
            if not isinstance(target, dict):
                return "Gateway Target 원장 항목 형식이 올바르지 않아요."
            sensitivity = str(
                target.get("sensitivity") or ""
            ).strip().upper()
            target_name = str(
                target.get("gatewayTargetName") or ""
            ).strip()
            target_id = str(
                target.get("gatewayTargetId") or ""
            ).strip()
            operations = target.get("operations")
            state = str(target.get("gatewayTargetState") or "").upper()
            if (
                not sensitivity
                or not target_name
                or not target_id
                or not isinstance(operations, list)
                or state != "READY"
            ):
                return "Gateway Target 원장 좌표나 관측 상태가 불완전해요."
            if sensitivity in seen_sensitivities:
                return (
                    "Gateway Target 원장에 같은 sensitivity가 중복돼요: "
                    f"{sensitivity}"
                )
            seen_sensitivities.add(sensitivity)
            if expected_names.get(sensitivity) != target_name:
                return (
                    "Gateway Target 이름이 파생 규칙과 일치하지 않아요: "
                    f"{target_name}"
                )
            for operation in operations:
                name = str(operation or "")
                if not name or name in memberships:
                    return "Gateway Target 원장에 중복되거나 빈 operation이 있어요."
                memberships[name] = sensitivity
        for name, _meta, sensitivity, _source in declared_tools(record):
            if sensitivity:
                if memberships.get(name) != sensitivity:
                    return (
                        "Gateway Target 원장이 민감도 태그와 일치하지 않아요: "
                        f"{name}"
                    )
            elif name in memberships:
                return (
                    "미분류 도구가 Gateway Target 원장에 남아 있어요: "
                    f"{name}"
                )
        return ""

    def _require_movable_target_ledger(
        self,
        record: RegistryRecord,
    ) -> None:
        if not self._is_deployed(record):
            return
        error = self._target_ledger_error(record)
        if error:
            raise SensitivityChangeRejected(
                "target_ledger_invalid",
                "Gateway Target 원장을 확인할 수 없어 민감도를 변경할 수 없어요.",
                remediation=error,
            )

    @staticmethod
    def _require_connected_catalog_ready(
        record: RegistryRecord,
        ledger: AssetDriftLedger,
    ) -> None:
        if (
            not McpDriftService._is_deployed(record)
            and (
                ledger.catalog_sync_pending
                or ledger.check_status is DriftCheckStatus.UNKNOWN
            )
        ):
            raise SensitivityChangeRejected(
                "connected_sync_pending",
                "연결형 Gateway Target의 최신 도구 목록을 아직 확인하지 못했어요.",
                remediation=(
                    ledger.check_error
                    or "Gateway Target 동기화가 완료된 뒤 다시 시도해 주세요."
                ),
            )

    def _require_change_target_axis(
        self,
        record: RegistryRecord,
        change: SensitivityChangeRequest,
    ) -> None:
        """Revalidate the live Registry axis without discarding retry evidence."""
        if not self._is_deployed(record):
            return
        mcp = self._mcp_node(record)
        raw_targets = mcp.get("gatewayTargets")
        if not isinstance(raw_targets, list):
            legacy_name = str(mcp.get("gatewayTargetName") or "").strip()
            if (
                "gatewayTargets" not in mcp
                and change.before == change.after
                and legacy_name
                and legacy_name == change.target_before
            ):
                return
            error = "진행 중인 변경의 legacy Target 축이 현재 원장과 일치하지 않아요."
        else:
            error = self._target_ledger_error(record)
            if not error:
                declared = {
                    name: sensitivity
                    for name, _meta, sensitivity, _source
                    in declared_tools(record)
                }
                if change.tool_name not in declared:
                    error = (
                        "현재 Registry descriptor에서 변경 중인 도구를 "
                        f"찾지 못했어요: {change.tool_name}"
                    )
                else:
                    current_sensitivity = declared[change.tool_name]
                    allowed_pairs = {
                        (sensitivity, target_name)
                        for sensitivity, target_name in (
                            (change.before, change.target_before),
                            (change.after, change.target_after),
                        )
                        if sensitivity and target_name
                    }
                    try:
                        selected = mcp_gateway_target_index(
                            record.descriptors
                        ).select((change.tool_name,))
                    except McpGatewayTargetError as exc:
                        selected = ()
                        if not (
                            current_sensitivity is None
                            and (
                                (change.before is None and change.target_before is None)
                                or (change.after is None and change.target_after is None)
                            )
                        ):
                            error = str(exc)
                    if selected and (
                        selected[0].sensitivity,
                        selected[0].name,
                    ) not in allowed_pairs:
                        error = (
                            "진행 중인 변경의 operation→Target 축이 현재 "
                            "원장과 일치하지 않아요."
                        )
        if error:
            raise SensitivityChangeRejected(
                "target_ledger_invalid",
                "Gateway Target 원장을 다시 확인할 수 없어 변경을 진행할 수 없어요.",
                remediation=error,
            )

    @staticmethod
    def _sensitivity_ledger_error(
        record: RegistryRecord,
        ledger: AssetDriftLedger,
    ) -> str:
        entries = {entry.tool_name: entry for entry in ledger.entries}
        for name, _meta, sensitivity, _source in declared_tools(record):
            entry = entries.get(name)
            if entry is None:
                return f"민감도 원장에 현재 배포 도구가 없어요: {name}"
            if entry.state in {
                ToolDriftState.MISSING,
                ToolDriftState.RETIRED,
            }:
                # Registry descriptor는 다음 배포가 커밋되기 전까지 옛 도구를 담을 수 있어요.
                # MISSING/RETIRED 원장의 빈 태그가 그 stale descriptor와 다른 것은 의도예요.
                continue
            if entry.sensitivity != sensitivity:
                return (
                    "Registry descriptor와 민감도 원장이 일치하지 않아요: "
                    f"{name}"
                )
        return ""

    def approve_sensitivity_change(
        self,
        record_id: str,
        tool_name: str,
        request_id: str,
        *,
        actor: str,
        actor_label: str = "",
    ) -> tuple[
        AssetToolDrift,
        SensitivityChangePlan,
        SensitivityChangeRequest,
    ]:
        record = self._record(record_id)
        ledger, entry = self._entry(record, tool_name)
        self._require_connected_catalog_ready(record, ledger)
        change = self._change(record, tool_name, request_id)
        if change.status is not SensitivityChangeStatus.PENDING_APPROVAL:
            raise McpSensitivityChangeConflict(request_id)
        plan = self._plan_pending(record, ledger, entry, change)
        if not self._agent_impact_known(plan):
            raise SensitivityChangeRejected(
                "impact_unknown",
                "영향받는 agent를 확인하지 못해 승인할 수 없어요.",
                remediation=self._agent_impact_reason(plan),
            )
        self._require_unchanged_approval_impact(
            change,
            plan,
            actor=actor,
            actor_label=actor_label,
        )
        return self._apply_change(
            record,
            ledger,
            entry,
            plan,
            change,
            actor=actor,
            actor_label=actor_label,
            approval_stage="approved",
            expected_status=SensitivityChangeStatus.PENDING_APPROVAL,
        )

    def retry_sensitivity_change(
        self,
        record_id: str,
        tool_name: str,
        request_id: str,
        *,
        actor: str,
        actor_label: str = "",
    ) -> tuple[
        AssetToolDrift,
        SensitivityChangePlan,
        SensitivityChangeRequest,
    ]:
        record = self._record(record_id)
        ledger, entry = self._entry(record, tool_name)
        self._require_connected_catalog_ready(record, ledger)
        change = self._change(record, tool_name, request_id)
        if (
            change.status is SensitivityChangeStatus.FAILED
            and change.retryable
        ):
            expected_status = SensitivityChangeStatus.FAILED
        elif (
            change.status is SensitivityChangeStatus.APPLYING
            and self._applying_retryable(change)
        ):
            expected_status = SensitivityChangeStatus.APPLYING
        elif (
            change.status
            is SensitivityChangeStatus.APPROVED_PENDING_PROPAGATION
            and self._target_propagation_enabled
        ):
            expected_status = (
                SensitivityChangeStatus.APPROVED_PENDING_PROPAGATION
            )
        else:
            raise McpSensitivityChangeConflict(request_id)
        plan = self._plan_pending(record, ledger, entry, change)
        if not self._agent_impact_known(plan):
            raise SensitivityChangeRejected(
                "impact_unknown",
                "영향받는 agent를 확인하지 못해 재시도할 수 없어요.",
                remediation=self._agent_impact_reason(plan),
            )
        return self._apply_change(
            record,
            ledger,
            entry,
            plan,
            change,
            actor=actor,
            actor_label=actor_label,
            approval_stage="retrying",
            expected_status=expected_status,
        )

    def _change(
        self,
        record: RegistryRecord,
        tool_name: str,
        request_id: str,
    ) -> SensitivityChangeRequest:
        get_change = getattr(self._store, "get_change", None)
        change = (
            get_change(asset_key_of(record), tool_name)
            if get_change is not None
            else None
        )
        if change is None or change.request_id != request_id:
            raise McpSensitivityChangeNotFound(request_id)
        return change

    def _plan_pending(
        self,
        record: RegistryRecord,
        ledger: AssetDriftLedger,
        entry: ToolLedgerEntry,
        change: SensitivityChangeRequest,
    ) -> SensitivityChangePlan:
        if entry.sensitivity != change.before:
            raise McpSensitivityChangeConflict(change.request_id)
        self._require_change_target_axis(record, change)
        legacy_split = (
            change.before == change.after
            and change.target_before
            and change.target_before != change.target_after
        )
        plan = plan_change(
            entry,
            change.after,
            impact=(
                self._legacy_impact(
                    record,
                    ledger.entries,
                    gateway_target_name=change.target_before,
                )
                if legacy_split
                else self._impact(
                    record,
                    change.tool_name,
                    gateway_target_names=self._impact_target_names(
                        record,
                        before=change.before,
                        after=change.after,
                        target_before=change.target_before,
                        target_after=change.target_after,
                    ),
                )
            ),
        )
        if (
            plan.before != change.before
            or plan.after != change.after
            or plan.downgrade != (change.direction == "downgrade")
        ):
            raise McpSensitivityChangeConflict(change.request_id)
        return plan

    @staticmethod
    def _agent_impact_snapshot(
        impact: tuple[dict, ...] | tuple[ImpactCount, ...],
    ) -> tuple:
        raw = next(
            (
                item.to_dict() if isinstance(item, ImpactCount) else item
                for item in impact
                if (
                    item.label.endswith("agent")
                    if isinstance(item, ImpactCount)
                    else str(item.get("label") or "").endswith("agent")
                )
            ),
            None,
        )
        if raw is None:
            return ("missing",)
        if not raw.get("known"):
            return ("unknown", str(raw.get("reason") or ""))
        agent_ids = tuple(sorted(
            str(value)
            for value in (raw.get("agent_ids") or ())
            if str(value)
        ))
        if agent_ids:
            return ("agent_ids", agent_ids)
        return (
            "names",
            tuple(sorted(str(value) for value in (raw.get("names") or ()))),
            int(raw.get("count") or 0),
        )

    def _require_unchanged_approval_impact(
        self,
        change: SensitivityChangeRequest,
        plan: SensitivityChangePlan,
        *,
        actor: str,
        actor_label: str,
    ) -> None:
        if self._agent_impact_snapshot(change.impact) == (
            self._agent_impact_snapshot(plan.impact)
        ):
            return
        now = self._now()
        refreshed = replace(
            change,
            impact=tuple(item.to_dict() for item in plan.impact),
        )
        try:
            refreshed = self._store.transition_change(
                refreshed,
                self._change_event(
                    refreshed,
                    stage="impact_changed",
                    actor=actor,
                    actor_label=actor_label,
                    at=now,
                ),
                expected_statuses=(
                    SensitivityChangeStatus.PENDING_APPROVAL,
                ),
            )
        except DriftChangeConflict as exc:
            raise McpSensitivityChangeConflict(change.request_id) from exc
        raise McpSensitivityImpactChanged(refreshed)

    @staticmethod
    def _pending_status(change: SensitivityChangeRequest | None) -> str:
        if change is None:
            return "blocked"
        return {
            SensitivityChangeStatus.PENDING_APPROVAL: "pending_approval",
            SensitivityChangeStatus.APPROVED_PENDING_PROPAGATION: (
                "pending_propagation"
            ),
            SensitivityChangeStatus.APPLYING: "applying",
            SensitivityChangeStatus.FAILED: "failed",
            SensitivityChangeStatus.APPLIED: "ready",
        }[change.status]

    def _pending_redeploy_decision(
        self,
        change: SensitivityChangeRequest | None,
    ) -> RedeploySensitivityDecision:
        if change is None:
            return {
                "status": "blocked",
                "reason": "민감도 변경 요청을 다시 읽지 못했어요.",
            }
        presented = self._present_change(change)
        decision: RedeploySensitivityDecision = {
            "status": self._pending_status(presented),
            "change": presented.to_dict(),
        }
        reason = presented.movement.get("reason")
        if isinstance(reason, str) and reason:
            decision["reason"] = reason
        return decision

    def _applying_retryable(self, change: SensitivityChangeRequest) -> bool:
        if change.status is not SensitivityChangeStatus.APPLYING:
            return False
        raw = (
            change.attempt_started_at
            or change.approved_at
            or change.requested_at
        )
        try:
            started = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            observed = datetime.fromisoformat(
                self._now().replace("Z", "+00:00")
            )
        except (AttributeError, TypeError, ValueError):
            return False
        return observed - started >= _APPLYING_RECOVERY_AFTER

    def _redeploy_change_retryable(
        self,
        change: SensitivityChangeRequest,
    ) -> bool:
        if not self._target_propagation_enabled:
            return False
        return (
            change.status
            is SensitivityChangeStatus.APPROVED_PENDING_PROPAGATION
        ) or self._applying_retryable(change)

    def _present_change(
        self,
        change: SensitivityChangeRequest,
    ) -> SensitivityChangeRequest:
        if not self._applying_retryable(change):
            return change
        return replace(
            change,
            retryable=True,
            error=(
                change.error
                or "이동 작업이 중단됐을 수 있어 재시도할 수 있어요."
            ),
        )

    @staticmethod
    def _agent_impact(plan: SensitivityChangePlan) -> ImpactCount | None:
        return next(
            (
                item
                for item in plan.impact
                if item.label.endswith("agent")
            ),
            None,
        )

    @classmethod
    def _agent_impact_known(cls, plan: SensitivityChangePlan) -> bool:
        impact = cls._agent_impact(plan)
        return impact is not None and impact.known

    @classmethod
    def _agent_impact_reason(cls, plan: SensitivityChangePlan) -> str:
        impact = cls._agent_impact(plan)
        return (
            impact.reason
            if impact is not None and impact.reason
            else "영향받는 agent를 관측하지 못했어요."
        )

    def _change_event(
        self,
        change: SensitivityChangeRequest,
        *,
        stage: str,
        actor: str,
        actor_label: str,
        at: str,
    ) -> SensitivityChangeEvent:
        return SensitivityChangeEvent(
            event_id=uuid4().hex,
            asset_key=change.asset_key,
            tool_name=change.tool_name,
            at=at,
            actor=actor or "unknown",
            actor_label=actor_label or "",
            before=change.before,
            after=change.after,
            before_source=change.before_source,
            after_source=(
                SensitivitySource.UNKNOWN.value
                if (
                    change.state_after == ToolDriftState.MISSING.value
                    and change.after is None
                )
                else (
                    change.before_source or SensitivitySource.UNKNOWN.value
                    if change.before == change.after
                    else SensitivitySource.ADMIN.value
                )
            ),
            state_before=change.state_before,
            state_after=change.state_after,
            downgrade=change.direction == "downgrade",
            reason=change.reason,
            groups_gained=change.groups_gained,
            groups_lost=change.groups_lost,
            request_id=change.request_id,
            stage=stage,
            change_status=change.status.value,
            impact=change.impact,
            movement=change.movement,
        )

    def _fail_before_apply(
        self,
        change: SensitivityChangeRequest,
        *,
        error: str,
        actor: str,
        actor_label: str,
    ) -> SensitivityChangeRequest:
        now = self._now()
        failed = replace(
            change,
            status=SensitivityChangeStatus.FAILED,
            error=error,
            retryable=True,
            movement={
                "status": "failed",
                "stage": "impact_observation",
                "error": error,
                "retryable": True,
                "coordinates": {
                    "before_target": change.target_before,
                    "after_target": change.target_after,
                },
            },
        )
        try:
            return self._store.transition_change(
                failed,
                self._change_event(
                    failed,
                    stage="failed",
                    actor=actor,
                    actor_label=actor_label,
                    at=now,
                ),
                expected_statuses=(
                    SensitivityChangeStatus.PENDING_APPROVAL,
                ),
            )
        except DriftChangeConflict as exc:
            raise McpSensitivityChangeConflict(change.request_id) from exc

    def _target_propagation_reason(
        self,
        record: RegistryRecord,
        change: SensitivityChangeRequest,
        plan: SensitivityChangePlan,
    ) -> str:
        impact = self._agent_impact(plan)
        if impact is None or not impact.known:
            impact_text = (
                "영향 agent 목록 확인 불가"
                + (f" ({impact.reason})" if impact and impact.reason else "")
            )
        elif impact.names:
            impact_text = (
                f"영향 agent {impact.count}개 ({', '.join(impact.names)})"
            )
        else:
            impact_text = f"영향 agent {impact.count}개"
        legacy_split = (
            change.before == change.after
            and change.target_before
            and change.target_before != change.target_after
        )
        if legacy_split:
            destinations = ", ".join(
                (
                    f"{tool_name}: {sensitivity or '미분류'} "
                    f"({self._expected_target(record, sensitivity) or 'Target 없음'})"
                )
                for tool_name, _meta, sensitivity, _source
                in declared_tools(record)
            )
            movement_text = (
                f"legacy Target {change.target_before}을 분할해야 해요: "
                f"{destinations}"
            )
        else:
            movement_text = (
                f"{change.tool_name}: {change.before or '미분류'} "
                f"({change.target_before or 'Target 없음'}) → "
                f"{change.after or '미분류'} "
                f"({change.target_after or 'Target 없음'}) 이동이 필요해요"
            )
        return (
            f"{movement_text}. {impact_text}. "
            f"agent binding, Cedar 정책, 생성 코드를 함께 갱신하는 "
            f"전파 경로가 아직 없어 {_TARGET_PROPAGATION_TICKET}까지 실행하지 않아요."
        )

    def _requires_target_propagation(
        self,
        record: RegistryRecord,
        change: SensitivityChangeRequest,
    ) -> bool:
        return (
            self._is_deployed(record)
            and change.target_before != change.target_after
        )

    def _record_approved_pending_propagation(
        self,
        record: RegistryRecord,
        ledger: AssetDriftLedger,
        plan: SensitivityChangePlan,
        change: SensitivityChangeRequest,
        *,
        actor: str,
        actor_label: str,
        approval_stage: str,
        expected_status: SensitivityChangeStatus,
    ) -> tuple[
        AssetToolDrift,
        SensitivityChangePlan,
        SensitivityChangeRequest,
    ]:
        approved_at = self._now()
        reason = self._target_propagation_reason(record, change, plan)
        pending = replace(
            change,
            status=SensitivityChangeStatus.APPROVED_PENDING_PROPAGATION,
            approved_by=change.approved_by or actor or "unknown",
            approved_by_label=(
                change.approved_by_label or actor_label or ""
            ),
            approved_at=change.approved_at or approved_at,
            ledger_version=ledger.version,
            impact=tuple(item.to_dict() for item in plan.impact),
            movement={
                "status": "blocked",
                "stage": "propagation_pending",
                "reason": reason,
                "ticket": _TARGET_PROPAGATION_TICKET,
                "retryable": False,
                "consistency": "unchanged",
                "coordinates": {
                    "before_target": change.target_before,
                    "after_target": change.target_after,
                },
            },
            error="",
            retryable=False,
        )
        try:
            pending = self._store.transition_change(
                pending,
                self._change_event(
                    pending,
                    stage=approval_stage,
                    actor=actor,
                    actor_label=actor_label,
                    at=approved_at,
                ),
                expected_statuses=(expected_status,),
            )
        except DriftChangeConflict as exc:
            raise McpSensitivityChangeConflict(change.request_id) from exc
        return self._to_snapshot(record, ledger), plan, pending

    def _apply_change(
        self,
        record: RegistryRecord,
        ledger: AssetDriftLedger,
        entry: ToolLedgerEntry,
        plan: SensitivityChangePlan,
        change: SensitivityChangeRequest,
        *,
        actor: str,
        actor_label: str,
        approval_stage: str,
        expected_status: SensitivityChangeStatus,
        connected_tool_definition: McpToolInfo | None = None,
    ) -> tuple[
        AssetToolDrift,
        SensitivityChangePlan,
        SensitivityChangeRequest,
    ]:
        if (
            self._requires_target_propagation(record, change)
            and not self._target_propagation_enabled
        ):
            if expected_status is not SensitivityChangeStatus.PENDING_APPROVAL:
                raise SensitivityChangeRejected(
                    "target_propagation_pending",
                    (
                        "기존 Target 이동의 실패·중단 증거를 보존하기 위해 "
                        "지금은 재시도하지 않아요."
                    ),
                    remediation=self._target_propagation_reason(
                        record,
                        change,
                        plan,
                    ),
                )
            return self._record_approved_pending_propagation(
                record,
                ledger,
                plan,
                change,
                actor=actor,
                actor_label=actor_label,
                approval_stage=approval_stage,
                expected_status=expected_status,
            )
        approval_at = self._now()
        applying = replace(
            change,
            status=SensitivityChangeStatus.APPLYING,
            approved_by=(
                change.approved_by or actor or "unknown"
            ),
            approved_by_label=(
                change.approved_by_label or actor_label or ""
            ),
            approved_at=change.approved_at or approval_at,
            attempt_started_at=approval_at,
            ledger_version=ledger.version,
            impact=tuple(item.to_dict() for item in plan.impact),
            error="",
        )
        try:
            applying = self._store.transition_change(
                applying,
                self._change_event(
                    applying,
                    stage=approval_stage,
                    actor=actor,
                    actor_label=actor_label,
                    at=approval_at,
                ),
                expected_statuses=(expected_status,),
            )
        except DriftChangeConflict as exc:
            raise McpSensitivityChangeConflict(change.request_id) from exc

        movement = self._move(record, applying)
        if movement.get("status") != "applied":
            if movement.get("stage") in {
                "not_wired",
                "descriptor_observation",
                "deployment_observation",
            }:
                movement = {
                    **movement,
                    "consistency": "unchanged",
                    "compensation": {"status": "not_applicable"},
                }
            else:
                movement = self._compensate_after_failure(
                    record,
                    applying,
                    movement,
                    restore_registry=False,
                )
            failed = self._record_failed_movement(
                applying,
                movement,
                actor=actor,
                actor_label=actor_label,
            )
            raise McpSensitivityPropagationFailed(failed)

        try:
            updated_record = self._update_registry_after_move(
                record,
                applying,
                movement,
                connected_tool_definition=connected_tool_definition,
            )
        except Exception as exc:
            failed_movement = self._compensate_after_failure(
                record,
                applying,
                {
                    **movement,
                    "status": "failed",
                    "stage": "registry_update",
                    "error": f"{type(exc).__name__}: {exc}",
                    "retryable": True,
                },
                restore_registry=True,
            )
            failed = self._record_failed_movement(
                applying,
                failed_movement,
                actor=actor,
                actor_label=actor_label,
            )
            raise McpSensitivityPropagationFailed(failed) from exc

        applied_at = self._now()
        updated = (
            entry
            if movement.get("mode") == "legacy_split"
            else confirm_sensitivity(
                entry,
                tag=plan.after,
                source=SensitivitySource.ADMIN.value,
                now=applied_at,
            )
        )
        entries = tuple(sorted(
            (
                updated if item.tool_name == plan.tool_name else item
                for item in ledger.entries
            ),
            key=lambda item: item.tool_name,
        ))
        saved = AssetDriftLedger(
            asset_key=ledger.asset_key or asset_key_of(record),
            check_status=ledger.check_status,
            last_checked_at=ledger.last_checked_at,
            check_error=ledger.check_error,
            catalog_sync_pending=ledger.catalog_sync_pending,
            catalog_snapshot_unobservable=(
                ledger.catalog_snapshot_unobservable
            ),
            entries=entries,
            version=ledger.version,
        )
        applied = replace(
            applying,
            status=SensitivityChangeStatus.APPLIED,
            applied_at=applied_at,
            movement=dict(movement),
            error="",
            retryable=False,
        )
        try:
            new_version, applied = self._store.commit_change(
                saved,
                applied,
                self._change_event(
                    applied,
                    stage="moved",
                    actor=actor,
                    actor_label=actor_label,
                    at=applied_at,
                ),
                expected_status=SensitivityChangeStatus.APPLYING,
            )
        except (DriftLedgerConflict, DriftChangeConflict) as exc:
            failed_movement = self._compensate_after_failure(
                record,
                applying,
                {
                    **movement,
                    "status": "failed",
                    "stage": "ledger_commit",
                    "error": f"{type(exc).__name__}: {exc}",
                    "retryable": True,
                },
                restore_registry=True,
            )
            failed = self._record_failed_movement(
                applying,
                failed_movement,
                actor=actor,
                actor_label=actor_label,
            )
            raise McpSensitivityPropagationFailed(failed) from exc
        saved = replace(saved, version=new_version)
        return self._to_snapshot(updated_record, saved), plan, applied

    def _move(
        self,
        record: RegistryRecord,
        change: SensitivityChangeRequest,
    ) -> SensitivityMovementResult:
        if not self._is_deployed(record):
            return {
                "status": "applied",
                "stage": "not_applicable",
                "retryable": False,
                "coordinates": {
                    "gateway_id": self._mcp_node(record).get(
                        "gatewayIdentifier"
                    ),
                    "before_target": None,
                    "after_target": None,
                },
            }
        if self._propagate is None:
            return {
                "status": "failed",
                "stage": "not_wired",
                "error": "Gateway Target 이동기가 배선되지 않았어요.",
                "retryable": True,
                "coordinates": {
                    "gateway_id": self._mcp_node(record).get(
                        "gatewayIdentifier"
                    ),
                    "before_target": change.target_before,
                    "after_target": change.target_after,
                },
            }
        request: SensitivityMovementRequest = {
            "kind": (
                "legacy_split"
                if (
                    change.before == change.after
                    and change.target_before
                    and change.target_before != change.target_after
                )
                else "sensitivity_change"
            ),
            "request_id": change.request_id,
            "record_id": record.record_id,
            "record_version": record.version,
            "asset_name": record.name,
            "tool_name": change.tool_name,
            "before": change.before,
            "after": change.after,
            "target_before": change.target_before,
            "target_after": change.target_after,
            "descriptors": copy.deepcopy(record.descriptors),
            "previous_movement": dict(change.movement),
        }
        try:
            result = self._propagate(request)
        except Exception as exc:
            return {
                "status": "failed",
                "stage": "move_tool",
                "error": f"{type(exc).__name__}: {exc}",
                "retryable": True,
                "coordinates": {
                    "gateway_id": self._mcp_node(record).get(
                        "gatewayIdentifier"
                    ),
                    "before_target": change.target_before,
                    "after_target": change.target_after,
                },
            }
        if not isinstance(result, dict):
            return {
                "status": "failed",
                "stage": "move_tool",
                "error": "Gateway Target 이동기가 올바른 결과를 반환하지 않았어요.",
                "retryable": True,
                "coordinates": {},
            }
        return result

    def _compensate_after_failure(
        self,
        record: RegistryRecord,
        change: SensitivityChangeRequest,
        failure: dict,
        *,
        restore_registry: bool,
    ) -> dict:
        """Best-effort reverse a completed live move and retain divergence proof."""
        legacy_split = (
            change.before == change.after
            and change.target_before
            and change.target_before != change.target_after
        )
        request: SensitivityMovementRequest = {
            "kind": (
                "legacy_restore"
                if legacy_split
                else "sensitivity_change"
            ),
            "request_id": f"{change.request_id}:compensate",
            "record_id": record.record_id,
            "record_version": record.version,
            "asset_name": record.name,
            "tool_name": change.tool_name,
            "before": change.after,
            "after": change.before,
            "target_before": change.target_after,
            "target_after": change.target_before,
            "descriptors": copy.deepcopy(record.descriptors),
            "previous_movement": dict(failure),
        }
        if self._propagate is None:
            live_result: dict = {
                "status": "failed",
                "stage": "compensation_not_wired",
                "error": "Gateway Target 보상 이동기가 배선되지 않았어요.",
                "retryable": True,
                "coordinates": {
                    "before_target": change.target_after,
                    "after_target": change.target_before,
                },
            }
        else:
            try:
                observed = self._propagate(request)
                live_result = (
                    dict(observed)
                    if isinstance(observed, dict)
                    else {
                        "status": "failed",
                        "stage": "compensate_move",
                        "error": "Gateway Target 보상 결과 형식이 올바르지 않아요.",
                        "retryable": True,
                        "coordinates": {},
                    }
                )
            except Exception as exc:
                live_result = {
                    "status": "failed",
                    "stage": "compensate_move",
                    "error": f"{type(exc).__name__}: {exc}",
                    "retryable": True,
                    "coordinates": {
                        "before_target": change.target_after,
                        "after_target": change.target_before,
                    },
                }

        registry_result: dict = {"status": "not_applicable"}
        if restore_registry:
            if not self._has_pre_move_registry_evidence(record, change):
                registry_result = {
                    "status": "failed",
                    "error": (
                        "pre-move Registry descriptor 증거가 없어 복구를 "
                        "완료했다고 판정할 수 없어요."
                    ),
                }
            else:
                try:
                    self._registry.update_record_descriptors(
                        self._registry_id,
                        record.record_id,
                        record.name,
                        record.descriptor_type,
                        copy.deepcopy(record.descriptors),
                        record.version,
                        description=record.description,
                    )
                    registry_result = {"status": "applied"}
                except Exception as exc:
                    registry_result = {
                        "status": "failed",
                        "error": f"{type(exc).__name__}: {exc}",
                    }

        restored = (
            live_result.get("status") == "applied"
            and registry_result.get("status")
            in {"applied", "not_applicable"}
        )
        errors = [
            str(result.get("error") or "")
            for result in (live_result, registry_result)
            if result.get("status") == "failed"
        ]
        compensation = {
            "status": "applied" if restored else "failed",
            "live": live_result,
            "registry": registry_result,
        }
        if errors:
            compensation["error"] = "; ".join(error for error in errors if error)
        return {
            **failure,
            "consistency": "restored" if restored else "diverged",
            "compensation": compensation,
        }

    @classmethod
    def _has_pre_move_registry_evidence(
        cls,
        record: RegistryRecord,
        change: SensitivityChangeRequest,
    ) -> bool:
        mcp = cls._mcp_node(record)
        legacy_split = (
            change.before == change.after
            and change.target_before
            and change.target_before != change.target_after
        )
        if legacy_split:
            return (
                "gatewayTargets" not in mcp
                and str(mcp.get("gatewayTargetName") or "").strip()
                == change.target_before
            )
        declared = {
            name: sensitivity
            for name, _meta, sensitivity, _source in declared_tools(record)
        }
        return (
            change.tool_name in declared
            and declared[change.tool_name] == change.before
        )

    def _record_failed_movement(
        self,
        change: SensitivityChangeRequest,
        movement: dict,
        *,
        actor: str,
        actor_label: str,
    ) -> SensitivityChangeRequest:
        failed_at = self._now()
        failed = replace(
            change,
            status=SensitivityChangeStatus.FAILED,
            movement=dict(movement),
            error=str(
                movement.get("error")
                or "Gateway Target 이동을 완료하지 못했어요."
            ),
            retryable=bool(movement.get("retryable", True)),
        )
        try:
            return self._store.transition_change(
                failed,
                self._change_event(
                    failed,
                    stage="failed",
                    actor=actor,
                    actor_label=actor_label,
                    at=failed_at,
                ),
                expected_statuses=(SensitivityChangeStatus.APPLYING,),
            )
        except DriftChangeConflict as exc:
            raise McpSensitivityChangeConflict(change.request_id) from exc

    @staticmethod
    def _mcp_node(record: RegistryRecord) -> dict:
        descriptors = (
            record.descriptors
            if isinstance(record.descriptors, dict)
            else {}
        )
        mcp = descriptors.get("mcp")
        return mcp if isinstance(mcp, dict) else {}

    @classmethod
    def _is_deployed(cls, record: RegistryRecord) -> bool:
        mcp = cls._mcp_node(record)
        return bool(mcp.get("sourcePrefix")) or isinstance(
            mcp.get("gatewayTargets"),
            list,
        )

    def _expected_target(
        self,
        record: RegistryRecord,
        sensitivity: str | None,
    ) -> str | None:
        if not sensitivity or not self._is_deployed(record):
            return None
        return self._target_name_resolver(record.name).get(sensitivity)

    def _impact_target_names(
        self,
        record: RegistryRecord,
        *,
        before: str | None,
        after: str | None,
        target_before: str | None = None,
        target_after: str | None = None,
    ) -> tuple[str | None, ...]:
        """Select the authorization-name axes without flattening split Targets."""
        mcp = self._mcp_node(record)
        if isinstance(mcp.get("gatewayTargets"), list):
            return (
                target_before or self._expected_target(record, before),
                target_after or self._expected_target(record, after),
            )
        legacy_name = str(mcp.get("gatewayTargetName") or "").strip()
        if legacy_name:
            return (legacy_name,)
        return (target_before, target_after)

    def _update_registry_after_move(
        self,
        record: RegistryRecord,
        change: SensitivityChangeRequest,
        movement: dict,
        *,
        connected_tool_definition: McpToolInfo | None = None,
    ) -> RegistryRecord:
        descriptors = copy.deepcopy(record.descriptors)
        mcp = descriptors.get("mcp")
        if not isinstance(mcp, dict):
            raise ValueError("MCP descriptor가 없어요.")
        if movement.get("mode") == "legacy_split":
            gateway_targets = movement.get("gateway_targets")
            unassigned_tools = movement.get("unassigned_tools")
            if not isinstance(gateway_targets, list):
                raise ValueError("이동 결과에 Gateway Target 원장이 없어요.")
            if not isinstance(unassigned_tools, list):
                raise ValueError("이동 결과에 미분류 도구 원장이 없어요.")
            mcp["gatewayTargets"] = gateway_targets
            mcp["unassignedTools"] = unassigned_tools
            mcp.pop("gatewayTargetName", None)
            mcp.pop("gatewayTargetId", None)
            mcp.pop("gatewayTargetState", None)
            return self._registry.update_record_descriptors(
                self._registry_id,
                record.record_id,
                record.name,
                record.descriptor_type,
                descriptors,
                record.version,
                description=record.description,
            )
        tools_node = mcp.get("tools")
        inline = (
            tools_node.get("inlineContent")
            if isinstance(tools_node, dict)
            else None
        )
        if not isinstance(inline, str):
            raise ValueError("MCP tool descriptor가 없어요.")
        document = json.loads(inline)
        tools = document.get("tools") if isinstance(document, dict) else None
        if not isinstance(tools, list):
            raise ValueError("MCP tool descriptor 형식이 올바르지 않아요.")
        found = False
        for tool in tools:
            if not isinstance(tool, dict) or tool.get("name") != change.tool_name:
                continue
            found = True
            if change.after:
                tool["sensitivity"] = change.after
                tool["sensitivitySource"] = SensitivitySource.ADMIN.value
            else:
                tool.pop("sensitivity", None)
                tool.pop("sensitivitySource", None)
        if not found:
            if self._is_deployed(record):
                raise ValueError(
                    "Registry descriptor에서 변경할 도구를 찾지 못했어요."
                )
            observed = (
                connected_tool_definition
                or self._connected_tool_definition(
                    record,
                    change.tool_name,
                )
            )
            tool = {
                "name": observed.name,
                "description": observed.description,
                "inputSchema": observed.input_schema,
            }
            if change.after:
                tool["sensitivity"] = change.after
                tool["sensitivitySource"] = SensitivitySource.ADMIN.value
            tools.append(tool)
        tools_node["inlineContent"] = json.dumps(
            document,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        sources = mcp.get("toolSensitivitySources")
        if not isinstance(sources, dict):
            sources = {}
            mcp["toolSensitivitySources"] = sources
        if change.after:
            sources[change.tool_name] = SensitivitySource.ADMIN.value
        else:
            sources.pop(change.tool_name, None)

        if self._is_deployed(record):
            gateway_targets = movement.get("gateway_targets")
            unassigned_tools = movement.get("unassigned_tools")
            if not isinstance(gateway_targets, list):
                raise ValueError("이동 결과에 Gateway Target 원장이 없어요.")
            if not isinstance(unassigned_tools, list):
                raise ValueError("이동 결과에 미분류 도구 원장이 없어요.")
            mcp["gatewayTargets"] = gateway_targets
            mcp["unassignedTools"] = unassigned_tools

        return self._registry.update_record_descriptors(
            self._registry_id,
            record.record_id,
            record.name,
            record.descriptor_type,
            descriptors,
            record.version,
            description=record.description,
        )

    def _prepare_connected_tool_definition(
        self,
        record: RegistryRecord,
        ledger: AssetDriftLedger,
        entry: ToolLedgerEntry,
    ) -> McpToolInfo | None:
        if self._is_deployed(record) or entry.tool_name in {
            name for name, _meta, _tag, _source in declared_tools(record)
        }:
            return None

        try:
            observed = self._connected_tool_definition(
                record,
                entry.tool_name,
            )
        except Exception as exc:
            reason = (
                "관리자 저장 직전 상류 도구 정의를 다시 확인하지 못했어요: "
                f"{type(exc).__name__}: {exc}"
            )
            self._mark_connected_catalog_pending(ledger, reason)
            raise SensitivityChangeRejected(
                "connected_sync_pending",
                "연결형 Gateway Target의 최신 도구 목록을 다시 동기화해야 해요.",
                remediation=reason,
            ) from exc

        fresh_shape = shape_of(
            observed.description,
            observed.input_schema,
        )
        if fresh_shape != entry.observed:
            reason = (
                "Gateway Target 동기화 뒤 상류 도구 정의가 다시 바뀌었어요. "
                "다시 읽어 동기화를 완료한 뒤 민감도를 저장해 주세요."
            )
            self._mark_connected_catalog_pending(ledger, reason)
            raise SensitivityChangeRejected(
                "connected_sync_pending",
                "연결형 Gateway Target의 최신 도구 목록을 다시 동기화해야 해요.",
                remediation=reason,
            )
        return observed

    def _mark_connected_catalog_pending(
        self,
        ledger: AssetDriftLedger,
        reason: str,
    ) -> None:
        pending = replace(
            ledger,
            check_status=DriftCheckStatus.UNKNOWN,
            check_error=reason,
            catalog_sync_pending=True,
        )
        try:
            self._store.put(pending)
        except DriftLedgerConflict:
            # A concurrent observer owns the newer catalog decision. The caller
            # still stops because this fresh probe cannot justify classification.
            return

    def _connected_tool_definition(
        self,
        record: RegistryRecord,
        tool_name: str,
    ) -> McpToolInfo:
        """Re-observe a connected-only tool before adding it to Registry.

        The drift ledger intentionally stores a bounded structural shape rather
        than a full JSON Schema. A tool discovered after registration therefore
        needs a fresh upstream observation when an administrator classifies it;
        copying the ledger's reduced shape would corrupt the Registry contract.
        """
        endpoint = upstream_endpoint_of(record)
        if not endpoint:
            raise ValueError(
                "연결형 MCP의 상류 endpoint를 확인하지 못했어요."
            )
        observed = self._fetch(endpoint)
        matches = [
            tool for tool in observed.tools
            if tool.name == tool_name
        ]
        if len(matches) != 1:
            raise ValueError(
                "상류 MCP에서 변경할 도구 정의를 유일하게 확인하지 못했어요."
            )
        return matches[0]

    def history(self, record_id: str) -> list[SensitivityChangeEvent]:
        record = self._record(record_id)
        return self._store.list_history(asset_key_of(record))

    def _impact(
        self,
        record: RegistryRecord,
        tool_name: str,
        *,
        sensitivity: str | None = None,
        gateway_target_name: str | None = None,
        gateway_target_names: tuple[str | None, ...] | None = None,
    ) -> tuple[ImpactCount, ...]:
        """영향 범위 — **원장에서 알 수 있는 것만**.

        `count_agents_bound_to_tool`은 인가 원장(agent × tool binding)을 세는 공용 seam
        이에요. 세지 못하면 숫자를 만들지 않고 "확인 불가"로 남겨요(ADR-0037 §4).
        """
        from ....shared.deps import count_agents_bound_to_tool

        if gateway_target_names is None and gateway_target_name is None:
            try:
                target_index = mcp_gateway_target_index(record.descriptors)
                if target_index.split:
                    target = target_index.select((tool_name,))[0]
                    if sensitivity and target.sensitivity != sensitivity:
                        raise McpGatewayTargetError(
                            "Gateway Target 민감도가 도구 원장과 일치하지 않아요."
                        )
                    gateway_target_name = target.name
                else:
                    gateway_target_name = (
                        target_index.legacy_target_name or ""
                    )
            except McpGatewayTargetError as e:
                observed = {
                    "known": False,
                    "reason": (
                        "Gateway Target 원장을 확인하지 못했어요: "
                        f"{e}"
                    ),
                }
                gateway_target_names = None
            else:
                gateway_target_names = (gateway_target_name,)
        elif gateway_target_names is None:
            gateway_target_names = (gateway_target_name,)

        targets = tuple(dict.fromkeys(
            str(target_name)
            for target_name in (gateway_target_names or ())
            if target_name
        ))
        observations: list[tuple[str, dict]] = []
        if gateway_target_names is not None:
            observed = {"known": True, "count": 0, "names": [], "agent_ids": []}
        for target_name in targets:
            try:
                current = count_agents_bound_to_tool(
                    gateway_target_name=target_name,
                    tool_name=tool_name,
                )
            except Exception as e:
                _log.warning(
                    "도구 영향 범위를 세지 못했어요: %s",
                    e,
                    exc_info=True,
                )
                current = {
                    "known": False,
                    "reason": f"인가 원장 조회에 실패했어요: {e}",
                }
            observations.append((target_name, current))

        unknown = [
            (target_name, item)
            for target_name, item in observations
            if not item.get("known")
        ]
        if unknown:
            observed = {
                "known": False,
                "reason": "; ".join(
                    f"{target_name}: "
                    f"{item.get('reason') or '확인하지 못했어요'}"
                    for target_name, item in unknown
                ),
            }
        elif observations:
            members: dict[tuple[str, str], tuple[str, str]] = {}
            for target_name, item in observations:
                names = [
                    str(value) for value in (item.get("names") or ())
                ]
                agent_ids = [
                    str(value) for value in (item.get("agent_ids") or ())
                ]
                if agent_ids and len(agent_ids) == len(names):
                    for agent_id, name in zip(agent_ids, names, strict=True):
                        members[("id", agent_id)] = (agent_id, name)
                else:
                    for name in names:
                        members[("name", name)] = ("", name)
                if int(item.get("count") or 0) > len(names):
                    observed = {
                        "known": False,
                        "reason": (
                            f"{target_name}: agent 식별자 목록이 합계보다 "
                            "불완전해 승인 스냅샷을 만들 수 없어요."
                        ),
                    }
                    break
            else:
                ordered = sorted(
                    members.values(),
                    key=lambda member: (member[0] or member[1], member[1]),
                )
                observed = {
                    "known": True,
                    "count": len(ordered),
                    "agent_ids": [
                        agent_id
                        for agent_id, _name in ordered
                        if agent_id
                    ],
                    "names": [name for _agent_id, name in ordered],
                }

        if observed.get("known"):
            agents = ImpactCount(
                label="이 도구를 인가받은 agent",
                known=True,
                count=int(observed.get("count") or 0),
                names=tuple(observed.get("names") or ()),
                agent_ids=tuple(observed.get("agent_ids") or ()),
            )
        else:
            agents = ImpactCount(
                label="이 도구를 인가받은 agent",
                known=False,
                reason=str(observed.get("reason") or "확인하지 못했어요"),
            )
        return (
            agents,
            # 사용자 수는 이 원장에 없어요. 태그는 등급 경계만 바꾸고, 등급을 가진 사람의
            # 수는 사용자 권한 원장 쪽 사실이에요 — 추정해서 채우지 않아요.
            ImpactCount(
                label="영향받는 사용자",
                known=False,
                reason=(
                    "도구 원장에는 사용자 정보가 없어요 — 관리자 콘솔 › 사용자 권한에서 "
                    "등급별 인원을 확인해 주세요."
                ),
            ),
        )

    def _legacy_impact(
        self,
        record: RegistryRecord,
        entries: tuple[ToolLedgerEntry, ...],
        *,
        gateway_target_name: str | None = None,
    ) -> tuple[ImpactCount, ...]:
        """Aggregate independently observed agent impact across a legacy Target."""
        if not gateway_target_name:
            gateway_target_name = str(
                self._mcp_node(record).get("gatewayTargetName") or ""
            )
        observations = [
            (
                entry.tool_name,
                self._impact(
                    record,
                    entry.tool_name,
                    gateway_target_names=(
                        gateway_target_name,
                        self._expected_target(record, entry.sensitivity),
                    ),
                ),
            )
            for entry in entries
        ]
        unknown = [
            (tool_name, impact[0])
            for tool_name, impact in observations
            if not impact[0].known
        ]
        if unknown:
            agents = ImpactCount(
                label="legacy Target 도구를 인가받은 agent",
                known=False,
                reason="; ".join(
                    f"{tool_name}: {impact.reason}"
                    for tool_name, impact in unknown
                ),
            )
        else:
            members: dict[tuple[str, str], tuple[str, str]] = {}
            for _tool_name, impact in observations:
                for index, name in enumerate(impact[0].names):
                    agent_id = (
                        impact[0].agent_ids[index]
                        if index < len(impact[0].agent_ids)
                        else ""
                    )
                    members[
                        ("id", agent_id) if agent_id else ("name", name)
                    ] = (agent_id, name)
            ordered = sorted(
                members.values(),
                key=lambda member: (member[0] or member[1], member[1]),
            )
            agents = ImpactCount(
                label="legacy Target 도구를 인가받은 agent",
                known=True,
                count=len(ordered),
                names=tuple(name for _agent_id, name in ordered),
                agent_ids=tuple(
                    agent_id
                    for agent_id, _name in ordered
                    if agent_id
                ),
            )
        return agents, observations[0][1][1]

    # --- 표현 --------------------------------------------------------------

    def _discard_obsolete_missing_changes(self, asset_key: str) -> None:
        discard = getattr(
            self._store,
            "discard_obsolete_missing_changes",
            None,
        )
        if discard is None:
            return
        removed = discard(asset_key)
        if removed:
            _log.info(
                "discarded obsolete MCP drift observer requests: "
                "asset=%s request_ids=%s",
                asset_key,
                ",".join(removed),
            )

    def _to_snapshot(self, record: RegistryRecord,
                     ledger: AssetDriftLedger) -> AssetToolDrift:
        mcp = record.descriptors.get("mcp") if isinstance(
            record.descriptors, dict
        ) else None
        deployed = isinstance(mcp, dict) and (
            bool(mcp.get("sourcePrefix"))
            or isinstance(mcp.get("gatewayTargets"), list)
        )
        asset_key = ledger.asset_key or asset_key_of(record)
        self._discard_obsolete_missing_changes(asset_key)
        list_changes = getattr(self._store, "list_changes", None)
        changes = (
            tuple(list_changes(asset_key))
            if list_changes is not None
            else ()
        )
        changes = tuple(self._present_change(change) for change in changes)
        return AssetToolDrift(
            asset_key=asset_key,
            record_id=record.record_id,
            asset_name=record.name,
            check_status=ledger.check_status,
            target_mode=(
                McpTargetMode.DEPLOYED
                if deployed
                else McpTargetMode.CONNECTED
            ),
            derived_target_names=(
                self._target_name_resolver(record.name) if deployed else {}
            ),
            changes=changes,
            last_checked_at=ledger.last_checked_at,
            check_error=ledger.check_error,
            catalog_snapshot_unobservable=(
                ledger.catalog_snapshot_unobservable
            ),
            entries=ledger.entries,
        )
