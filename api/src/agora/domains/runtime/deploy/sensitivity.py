"""Move deployed MCP tools between sensitivity-derived Gateway Targets."""

from __future__ import annotations

import copy
import json
import time

from ....shared.sensitivity_movement import (
    SensitivityMovementRequest,
    SensitivityMovementResult,
)
from .models import DeployPhase
from .target_slices import TargetSlice, split_target_slices


class SensitivityTargetMover:
    """Apply one tool membership change through the existing DeployPort."""

    def __init__(
        self,
        *,
        store,
        port,
        sleep=None,
        max_status_attempts: int = 40,
    ) -> None:
        self._store = store
        self._port = port
        self._sleep = sleep or time.sleep
        self._max_status_attempts = max(1, max_status_attempts)

    def __call__(
        self,
        request: SensitivityMovementRequest,
    ) -> SensitivityMovementResult:
        coordinates = self._coordinates(request)
        try:
            gateway_id, inline = self._descriptor_coordinates(request)
            coordinates["gateway_id"] = gateway_id
        except Exception as exc:
            return self._failure(
                "descriptor_observation",
                f"{type(exc).__name__}: {exc}",
                coordinates,
            )
        try:
            job = self._latest_ready_job(request)
            lambda_arn = str(job.lambda_arn or "")
            coordinates["lambda_arn"] = lambda_arn
        except Exception as exc:
            return self._failure(
                "deployment_observation",
                f"{type(exc).__name__}: {exc}",
                coordinates,
            )
        try:
            desired = self._desired_slices(request, inline)
            by_sensitivity = {
                target_slice.sensitivity: target_slice
                for target_slice in desired.slices
            }
            if request.get("kind") == "legacy_split":
                return self._move_legacy_target(
                    request=request,
                    gateway_id=gateway_id,
                    lambda_arn=lambda_arn,
                    desired=desired,
                    coordinates=coordinates,
                )
            if request.get("kind") == "legacy_restore":
                return self._restore_legacy_target(
                    request=request,
                    gateway_id=gateway_id,
                    lambda_arn=lambda_arn,
                    tools_inline=inline,
                    desired=desired,
                    coordinates=coordinates,
                )

            before_name = str(request.get("target_before") or "")
            after_name = str(request.get("target_after") or "")
            if before_name and before_name != after_name:
                failure = self._remove_old_membership(
                    gateway_id=gateway_id,
                    lambda_arn=lambda_arn,
                    target_name=before_name,
                    desired_slice=by_sensitivity.get(
                        str(request.get("before") or "")
                    ),
                    coordinates=coordinates,
                )
                if failure is not None:
                    return failure

            if after_name:
                destination = by_sensitivity.get(
                    str(request.get("after") or "")
                )
                if destination is None:
                    return self._failure(
                        "add_new_target",
                        "목적지 Target 슬라이스를 계산하지 못했어요.",
                        coordinates,
                    )
                failure = self._ensure_destination(
                    gateway_id=gateway_id,
                    lambda_arn=lambda_arn,
                    target_slice=destination,
                    request_id=str(request.get("request_id") or ""),
                    coordinates=coordinates,
                )
                if failure is not None:
                    return failure

            observed, failure = self._observe_complete_ledger(
                gateway_id,
                lambda_arn,
                desired.slices,
                coordinates,
            )
            if failure is not None:
                return failure
            return {
                "status": "applied",
                "stage": "completed",
                "retryable": False,
                "coordinates": coordinates,
                "gateway_targets": observed,
                "unassigned_tools": [
                    dict(item) for item in desired.unassigned_tools
                ],
            }
        except Exception as exc:
            return self._failure(
                "movement_observation",
                f"{type(exc).__name__}: {exc}",
                coordinates,
            )

    def _move_legacy_target(
        self,
        *,
        request: SensitivityMovementRequest,
        gateway_id: str,
        lambda_arn: str,
        desired,
        coordinates: dict,
    ) -> dict:
        legacy_name = str(request.get("target_before") or "")
        if not legacy_name:
            return self._failure(
                "remove_legacy_target",
                "legacy Target 이름이 없어요.",
                coordinates,
            )
        try:
            legacy_id = self._port.find_gateway_target(
                gateway_id,
                legacy_name,
            )
            coordinates["before_target_id"] = legacy_id
            if legacy_id:
                self._port.delete_target(gateway_id, legacy_id)
                failure = self._wait_absent(
                    gateway_id,
                    legacy_name,
                    stage="remove_legacy_target",
                    coordinates=coordinates,
                )
                if failure is not None:
                    return failure
        except Exception as exc:
            return self._failure(
                "remove_legacy_target",
                f"{type(exc).__name__}: {exc}",
                coordinates,
            )

        target_ids: dict[str, str] = {}
        for target_slice in desired.slices:
            failure = self._ensure_destination(
                gateway_id=gateway_id,
                lambda_arn=lambda_arn,
                target_slice=target_slice,
                request_id=str(request.get("request_id") or ""),
                coordinates=coordinates,
            )
            if failure is not None:
                failure["stage"] = "create_split_targets"
                failure["coordinates"]["target_ids"] = target_ids
                return failure
            target_ids[target_slice.gateway_target_name] = str(
                coordinates.get("after_target_id") or ""
            )

        observed, failure = self._observe_complete_ledger(
            gateway_id,
            lambda_arn,
            desired.slices,
            coordinates,
        )
        if failure is not None:
            failure["coordinates"]["target_ids"] = target_ids
            return failure
        coordinates["target_ids"] = target_ids
        return {
            "status": "applied",
            "stage": "completed",
            "mode": "legacy_split",
            "retryable": False,
            "coordinates": coordinates,
            "gateway_targets": observed,
            "unassigned_tools": [
                dict(item) for item in desired.unassigned_tools
            ],
        }

    def _restore_legacy_target(
        self,
        *,
        request: SensitivityMovementRequest,
        gateway_id: str,
        lambda_arn: str,
        tools_inline: str,
        desired,
        coordinates: dict,
    ) -> dict:
        legacy_name = str(request.get("target_after") or "")
        if not legacy_name:
            return self._failure(
                "restore_legacy_target",
                "복구할 legacy Target 이름이 없어요.",
                coordinates,
            )
        try:
            for target_slice in desired.slices:
                target_id = self._port.find_gateway_target(
                    gateway_id,
                    target_slice.gateway_target_name,
                )
                if not target_id:
                    continue
                self._port.delete_target(gateway_id, target_id)
                failure = self._wait_absent(
                    gateway_id,
                    target_slice.gateway_target_name,
                    stage="remove_split_targets",
                    coordinates=coordinates,
                )
                if failure is not None:
                    return failure

            legacy_id = self._port.find_gateway_target(
                gateway_id,
                legacy_name,
            )
            if legacy_id:
                self._port.update_gateway_target(
                    gateway_id,
                    legacy_id,
                    legacy_name,
                    lambda_arn,
                    tools_inline,
                )
            else:
                legacy_id = self._port.wire_lambda_target(
                    gateway_id,
                    legacy_name,
                    lambda_arn,
                    tools_inline,
                    client_token_seed=str(request.get("request_id") or ""),
                )
            coordinates["after_target_id"] = legacy_id
            failure = self._wait_ready(
                gateway_id,
                legacy_id,
                target_name=legacy_name,
                stage="restore_legacy_target",
                coordinates=coordinates,
            )
            if failure is not None:
                return failure
            changed = self._port.update_gateway_target(
                gateway_id,
                legacy_id,
                legacy_name,
                lambda_arn,
                tools_inline,
            )
            if changed:
                return self._failure(
                    "restore_legacy_target",
                    "legacy Target의 live 도구 구성이 원본과 일치하지 않아요.",
                    coordinates,
                )
        except Exception as exc:
            return self._failure(
                "restore_legacy_target",
                f"{type(exc).__name__}: {exc}",
                coordinates,
            )
        return {
            "status": "applied",
            "stage": "compensated",
            "mode": "legacy_restore",
            "retryable": False,
            "coordinates": coordinates,
        }

    @staticmethod
    def _coordinates(request: SensitivityMovementRequest) -> dict:
        previous = request.get("previous_movement")
        prior_coordinates = (
            previous.get("coordinates")
            if isinstance(previous, dict)
            and isinstance(previous.get("coordinates"), dict)
            else {}
        )
        return {
            **prior_coordinates,
            "record_id": str(request.get("record_id") or ""),
            "record_version": str(request.get("record_version") or ""),
            "before_target": request.get("target_before"),
            "after_target": request.get("target_after"),
        }

    @staticmethod
    def _descriptor_coordinates(
        request: SensitivityMovementRequest,
    ) -> tuple[str, str]:
        descriptors = request.get("descriptors")
        mcp = descriptors.get("mcp") if isinstance(descriptors, dict) else None
        if not isinstance(mcp, dict):
            raise ValueError("MCP descriptor가 없어요.")
        gateway_id = str(mcp.get("gatewayIdentifier") or "")
        tools = mcp.get("tools")
        inline = (
            tools.get("inlineContent")
            if isinstance(tools, dict)
            else None
        )
        if not gateway_id:
            raise ValueError("Gateway 식별자가 없어요.")
        if not isinstance(inline, str) or not inline:
            raise ValueError("MCP tool descriptor가 없어요.")
        return gateway_id, inline

    def _latest_ready_job(self, request: SensitivityMovementRequest):
        record_id = str(request.get("record_id") or "")
        record_version = str(request.get("record_version") or "")
        candidates = [
            job
            for job in self._store.list()
            if (
                job.phase is DeployPhase.READY
                and job.asset_type == "mcp"
                and job.lambda_arn
                and record_id in {
                    str(job.record_id or ""),
                    str(job.redeploy_record_id or ""),
                }
                and job.source_ref.version == record_version
            )
        ]
        if not candidates:
            raise LookupError(
                "현재 Registry 버전에 대응하는 READY MCP 배포를 찾지 못했어요."
            )
        return max(
            candidates,
            key=lambda job: (
                job.updated_at,
                job.created_at,
                job.job_id,
            ),
        )

    @staticmethod
    def _desired_slices(
        request: SensitivityMovementRequest,
        inline: str,
    ):
        document = json.loads(inline)
        tools = document.get("tools") if isinstance(document, dict) else None
        if not isinstance(tools, list):
            raise ValueError("MCP tool descriptor 형식이 올바르지 않아요.")
        desired_document = copy.deepcopy(document)
        desired_tools = desired_document["tools"]
        found = False
        for tool in desired_tools:
            if not isinstance(tool, dict):
                continue
            if tool.get("name") != request.get("tool_name"):
                continue
            found = True
            after = request.get("after")
            if after:
                tool["sensitivity"] = str(after)
            else:
                tool.pop("sensitivity", None)
            tool.pop("sensitivitySource", None)
        if not found:
            raise ValueError("이동할 도구를 descriptor에서 찾지 못했어요.")
        return split_target_slices(
            str(request.get("asset_name") or ""),
            json.dumps(
                desired_document,
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        )

    def _remove_old_membership(
        self,
        *,
        gateway_id: str,
        lambda_arn: str,
        target_name: str,
        desired_slice: TargetSlice | None,
        coordinates: dict,
    ) -> dict | None:
        try:
            target_id = self._port.find_gateway_target(
                gateway_id,
                target_name,
            )
            coordinates["before_target_id"] = target_id
            if not target_id:
                return None
            if desired_slice is None:
                self._port.delete_target(gateway_id, target_id)
                return self._wait_absent(
                    gateway_id,
                    target_name,
                    stage="remove_old_target",
                    coordinates=coordinates,
                )

            self._port.update_gateway_target(
                gateway_id,
                target_id,
                target_name,
                lambda_arn,
                desired_slice.tools_inline,
            )
            failure = self._wait_ready(
                gateway_id,
                target_id,
                target_name=target_name,
                stage="remove_old_target",
                coordinates=coordinates,
            )
            if failure is not None:
                return failure
            return self._verify_membership(
                gateway_id=gateway_id,
                target_id=target_id,
                target_name=target_name,
                lambda_arn=lambda_arn,
                target_slice=desired_slice,
                coordinates=coordinates,
            )
        except Exception as exc:
            return self._failure(
                "remove_old_target",
                f"{type(exc).__name__}: {exc}",
                coordinates,
            )

    def _ensure_destination(
        self,
        *,
        gateway_id: str,
        lambda_arn: str,
        target_slice: TargetSlice,
        request_id: str,
        coordinates: dict,
    ) -> dict | None:
        target_id = self._port.find_gateway_target(
            gateway_id,
            target_slice.gateway_target_name,
        )
        if target_id:
            self._port.update_gateway_target(
                gateway_id,
                target_id,
                target_slice.gateway_target_name,
                lambda_arn,
                target_slice.tools_inline,
            )
        else:
            target_id = self._port.wire_lambda_target(
                gateway_id,
                target_slice.gateway_target_name,
                lambda_arn,
                target_slice.tools_inline,
                client_token_seed=request_id,
            )
        coordinates["after_target_id"] = target_id
        failure = self._wait_ready(
            gateway_id,
            target_id,
            target_name=target_slice.gateway_target_name,
            stage="add_new_target",
            coordinates=coordinates,
        )
        if failure is not None:
            return failure
        return self._verify_membership(
            gateway_id=gateway_id,
            target_id=target_id,
            target_name=target_slice.gateway_target_name,
            lambda_arn=lambda_arn,
            target_slice=target_slice,
            coordinates=coordinates,
        )

    def _observe_complete_ledger(
        self,
        gateway_id: str,
        lambda_arn: str,
        slices: tuple[TargetSlice, ...],
        coordinates: dict,
    ) -> tuple[list[dict], dict | None]:
        observed: list[dict] = []
        for target_slice in slices:
            target_id = self._port.find_gateway_target(
                gateway_id,
                target_slice.gateway_target_name,
            )
            if not target_id:
                return [], self._failure(
                    "final_inventory",
                    f"Target을 관측하지 못했어요: "
                    f"{target_slice.gateway_target_name}",
                    coordinates,
                )
            failure = self._wait_ready(
                gateway_id,
                target_id,
                target_name=target_slice.gateway_target_name,
                stage="final_inventory",
                coordinates=coordinates,
            )
            if failure is not None:
                return [], failure
            failure = self._verify_membership(
                gateway_id=gateway_id,
                target_id=target_id,
                target_name=target_slice.gateway_target_name,
                lambda_arn=lambda_arn,
                target_slice=target_slice,
                coordinates=coordinates,
            )
            if failure is not None:
                return [], failure
            observed.append({
                "sensitivity": target_slice.sensitivity,
                "gatewayTargetName": target_slice.gateway_target_name,
                "gatewayTargetId": target_id,
                "gatewayTargetState": "ready",
                "operations": list(target_slice.operations),
            })
        return observed, None

    def _wait_ready(
        self,
        gateway_id: str,
        target_id: str,
        *,
        target_name: str,
        stage: str,
        coordinates: dict,
    ) -> dict | None:
        for attempt in range(self._max_status_attempts):
            try:
                status = self._port.get_target_status(gateway_id, target_id)
            except Exception as exc:
                return self._failure(
                    stage,
                    f"{type(exc).__name__}: {exc}",
                    coordinates,
                )
            if status.state == "READY":
                return None
            if status.state != "SYNCHRONIZING":
                return self._failure(
                    stage,
                    status.reason or f"{target_name} 상태가 {status.state}예요.",
                    coordinates,
                )
            if attempt + 1 < self._max_status_attempts:
                self._sleep(3)
        return self._failure(
            stage,
            f"{target_name} READY 상태를 제한 시간 안에 관측하지 못했어요.",
            coordinates,
        )

    def _wait_absent(
        self,
        gateway_id: str,
        target_name: str,
        *,
        stage: str,
        coordinates: dict,
    ) -> dict | None:
        for attempt in range(self._max_status_attempts):
            try:
                if not self._port.find_gateway_target(gateway_id, target_name):
                    return None
            except Exception as exc:
                return self._failure(
                    stage,
                    f"{type(exc).__name__}: {exc}",
                    coordinates,
                )
            if attempt + 1 < self._max_status_attempts:
                self._sleep(3)
        return self._failure(
            stage,
            f"{target_name} 삭제를 제한 시간 안에 관측하지 못했어요.",
            coordinates,
        )

    def _verify_membership(
        self,
        *,
        gateway_id: str,
        target_id: str,
        target_name: str,
        lambda_arn: str,
        target_slice: TargetSlice,
        coordinates: dict,
    ) -> dict | None:
        """Require a live GetGatewayTarget comparison to match the desired schema."""
        try:
            changed = self._port.update_gateway_target(
                gateway_id,
                target_id,
                target_name,
                lambda_arn,
                target_slice.tools_inline,
            )
            if not changed:
                return None
            failure = self._wait_ready(
                gateway_id,
                target_id,
                target_name=target_name,
                stage="verify_target_membership",
                coordinates=coordinates,
            )
            if failure is not None:
                return failure
            changed_again = self._port.update_gateway_target(
                gateway_id,
                target_id,
                target_name,
                lambda_arn,
                target_slice.tools_inline,
            )
            if not changed_again:
                return None
        except Exception as exc:
            return self._failure(
                "verify_target_membership",
                f"{type(exc).__name__}: {exc}",
                coordinates,
            )
        return self._failure(
            "verify_target_membership",
            f"{target_name}의 live 도구 구성이 기대값과 일치함을 관측하지 못했어요.",
            coordinates,
        )

    @staticmethod
    def _failure(
        stage: str,
        error: str,
        coordinates: dict,
    ) -> SensitivityMovementResult:
        return {
            "status": "failed",
            "stage": stage,
            "error": error,
            "retryable": True,
            "coordinates": dict(coordinates),
        }
