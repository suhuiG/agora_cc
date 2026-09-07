"""자산의 소스 스캔 적용성 해석.

Registry descriptor가 우선 근거이고, legacy agent만 SourceStore version 메타를 조회해요.
관측 실패는 ``unknown``으로 보존하며 통과나 해당없음으로 접지 않아요.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache


INITIALIZR_NOT_APPLICABLE_REASON = "Initializr 배포 — 소스 스캔 대상 아님"
UNKNOWN_APPLICABILITY_REASON = "소스 출처를 관측하지 못해 스캔 적용 여부를 확인할 수 없음"


@dataclass(frozen=True)
class ScanApplicability:
    state: str
    reason: str = ""

    def as_dict(self) -> dict[str, str]:
        return {"state": self.state, "reason": self.reason}


class ScanNotApplicableError(ValueError):
    """소스 스캔 대상이 아닌 자산에 스캔을 요청했어요."""


@lru_cache(maxsize=512)
def _source_deployment_source(
    source_store,
    asset_id: str,
    version: str,
    registry_generation: str,
) -> str:
    """관측된 출처만 Registry 세대 동안 재사용해요.

    SourceStore 좌표는 rollback/purge 뒤 재사용될 수 있으므로 asset/version만 캐시하면
    안 돼요. Registry record_id+updated_at 세대를 키에 포함해 재등록을 무효화하고,
    누락·빈 메타·예외는 캐시하지 않아 다음 poll에서 다시 관측해요.
    """
    del registry_generation  # cache key로만 사용해요.
    source_version = next(
        (
            item
            for item in source_store.list_versions(asset_id)
            if item.version == version
        ),
        None,
    )
    deployment_source = str(
        (getattr(source_version, "meta", {}) or {}).get("deployment_source") or ""
    )
    if not deployment_source:
        raise LookupError(f"deployment source unavailable: {asset_id}@{version}")
    return deployment_source


def scan_applicability(record, source_store=None) -> ScanApplicability:
    """Registry 선언과 SourceStore 관측으로 소스 스캔 적용성을 판정해요."""
    descriptors = getattr(record, "descriptors", {}) or {}
    agent = descriptors.get("agent") if isinstance(descriptors, dict) else None
    descriptor_type = getattr(record, "descriptor_type", None)
    descriptor_type = (
        descriptor_type.value
        if hasattr(descriptor_type, "value")
        else str(descriptor_type or "")
    )
    if not isinstance(agent, dict) and descriptor_type != "Agent":
        return ScanApplicability("applicable")

    agent = agent if isinstance(agent, dict) else {}
    binding = agent.get("executionBinding")
    if isinstance(binding, dict) and binding.get("blueprintId"):
        return ScanApplicability(
            "not_applicable",
            INITIALIZR_NOT_APPLICABLE_REASON,
        )
    if agent.get("deploymentSource") == "initializr":
        return ScanApplicability(
            "not_applicable",
            INITIALIZR_NOT_APPLICABLE_REASON,
        )

    prefix = str(
        agent.get("sourcePrefix")
        or getattr(record, "source_prefix", "")
        or ""
    )
    parts = [part for part in prefix.split("/") if part]
    if len(parts) < 4:
        return ScanApplicability("applicable")

    asset_id = "/".join(parts[1:-1])
    version = parts[-1]
    try:
        if source_store is None:
            from ...shared.deps import get_source_store

            source_store = get_source_store()
        generation = ":".join((
            str(getattr(record, "record_id", "") or ""),
            str(getattr(record, "updated_at", "") or ""),
        ))
        deployment_source = _source_deployment_source(
            source_store,
            asset_id,
            version,
            generation,
        )
    except Exception:
        return ScanApplicability("unknown", UNKNOWN_APPLICABILITY_REASON)

    if deployment_source == "initializr":
        return ScanApplicability(
            "not_applicable",
            INITIALIZR_NOT_APPLICABLE_REASON,
        )
    if deployment_source == "catalog":
        return ScanApplicability("applicable")
    return ScanApplicability("unknown", UNKNOWN_APPLICABILITY_REASON)


def scan_applicability_for_record(
    record_id: str,
    *,
    registry=None,
    registry_id: str | None = None,
    source_store=None,
) -> ScanApplicability:
    """record_id 경계에서 적용성을 읽어요. Registry 미관측도 unknown이에요."""
    try:
        if registry is None or registry_id is None:
            from ...shared.deps import get_registry, get_registry_id

            registry = registry or get_registry()
            registry_id = registry_id or get_registry_id()
        record = registry.get_record(registry_id, record_id)
    except Exception:
        return ScanApplicability(
            "unknown",
            "자산 정보를 관측하지 못해 스캔 적용 여부를 확인할 수 없음",
        )
    return scan_applicability(record, source_store)


def require_scan_applicable(
    record_id: str,
    *,
    registry=None,
    registry_id: str | None = None,
    source_store=None,
) -> ScanApplicability:
    applicability = scan_applicability_for_record(
        record_id,
        registry=registry,
        registry_id=registry_id,
        source_store=source_store,
    )
    if applicability.state == "not_applicable":
        raise ScanNotApplicableError(
            applicability.reason or INITIALIZR_NOT_APPLICABLE_REASON
        )
    return applicability
