"""자산 인벤토리 집계 (W5) — "무엇을 갖고 있고 무엇이 방치됐나".

기존 `dashboard_router`(심사 관측 축: 게이트 통과율·위험분포·승인 KPI)와 **별도 엔드포인트**
예요. 두 화면이 답하는 질문이 달라서 합치면 어느 위젯이 어느 축인지 흐려지고, 인벤토리의
중복검토 조회 장애가 게이트 통과율 위젯까지 깨요.

집계 소스는 `dashboard_router._queue_annotations()`를 재사용해요 — 새로 만들면 두 화면 숫자가
어긋나요(대시보드가 큐와 어긋났던 결함의 재발, `dashboard_router.py`의 approvals 주석 참조).

**측정 축은 아직 데이터가 없어요.** 호출수·토큰·비용·품질은 Evaluation telemetry가, 마지막
사용은 사용 추적이, owner 상태는 인사시스템 연동이 선행이에요. 그 사실을 `measurement`로
명시해요 — 프론트가 "값이 비었다"와 "측정을 안 한다"를 구분해야 하고, 프론트에 하드코딩하면
telemetry가 붙을 때 두 곳을 고쳐야 하니까요.
"""
from __future__ import annotations

import logging
from decimal import Decimal

from fastapi import APIRouter, Depends

from ...shared.config import load_config
from ...shared.deps import get_deploy_service, get_gov_store
from .authz import CONSOLE_ROLES, require_role
from .dashboard_router import _queue_annotations

router = APIRouter(tags=["governance-inventory"])

_log = logging.getLogger(__name__)

# 측정 축과 그 선행 조건. 값이 `not_implemented`인 동안 프론트는 회색 `—` + "측정 미구현"
# 배지를 그려요. 데이터가 생기면 `available`로 바꾸고, 그때부터 빈칸이 진짜 risk예요.
_MEASUREMENT_AXES = (
    "invocations", "tokens", "cost", "quality", "last_used", "owner_status",
)

# 중복 후보 카드에 실을 상한. 상위 점수만 보여도 "통합 검토" 판단에 충분하고, low band
# 수십 건이 카드를 채우면 정작 high가 묻혀요(overlap_review._MAX_CANDIDATES와 같은 취지).
_MAX_CARD_CANDIDATES = 20


def _authorization_enforcement(row: dict, config) -> tuple[str, str]:
    """Registry descriptor + stack-owned runtime mode를 합성하되 미관측은 unknown으로 둬요."""
    if row.get("asset_type") != "MCP":
        return "not_applicable", "registry_descriptor"
    descriptors = row.get("descriptors")
    mcp = descriptors.get("mcp") if isinstance(descriptors, dict) else None
    if not isinstance(mcp, dict):
        return "unknown", "registry_descriptor"

    gateway_id = mcp.get("gatewayIdentifier")
    target_id = mcp.get("gatewayTargetId")
    split_targets = mcp.get("gatewayTargets")
    upstream = mcp.get("upstreamEndpoint")
    if (
        isinstance(gateway_id, str)
        and gateway_id
        and gateway_id == config.m2_oauth_gateway_id
    ):
        if isinstance(split_targets, list):
            if not split_targets or any(
                not isinstance(target, dict)
                or not target.get("gatewayTargetId")
                or target.get("gatewayTargetState") != "ready"
                for target in split_targets
            ):
                return "unknown", "registry_descriptor"
        elif (
            mcp.get("gatewayTargetState") not in (None, "ready")
            or not target_id
        ):
            return "unknown", "registry_descriptor"
        if config.m2_oauth_gateway_mode == "ENFORCE":
            return "enforcing", "runtime_configuration"
        if config.m2_oauth_gateway_mode == "LOG_ONLY":
            return "log_only", "runtime_configuration"
        return "unknown", "runtime_configuration"
    if target_id or isinstance(split_targets, list) or upstream or mcp.get("endpoint"):
        return "unmanaged", "registry_descriptor"
    return "unknown", "registry_descriptor"


def _gateway_target_state(row: dict) -> str:
    """이 MCP 자산의 Gateway target 실체 상태 (CA-32 ②).

    * `ready` — target 좌표가 descriptor 에 확정돼 있어요.
    * `unknown` — target 원장이 비었거나 불완전해 실체를 확인할 수 없어요.
    * `not_applicable` — MCP가 아니거나 Agora Target 관리 대상이 아니에요.

    자동으로 지우지 않아요 — 드러내기만 해요. 삭제는 관리자 결정이에요.
    """
    if row.get("asset_type") != "MCP":
        return "not_applicable"
    descriptors = row.get("descriptors")
    mcp = descriptors.get("mcp") if isinstance(descriptors, dict) else None
    if not isinstance(mcp, dict):
        return "unknown"
    if mcp.get("gatewayTargetState") == "provisioning":
        return "unknown"
    split_targets = mcp.get("gatewayTargets")
    if isinstance(split_targets, list):
        if not split_targets:
            return "unknown"
        if all(
            isinstance(target, dict)
            and target.get("gatewayTargetId")
            and target.get("gatewayTargetState") == "ready"
            for target in split_targets
        ):
            return "ready"
        return "unknown"
    if mcp.get("gatewayTargetId"):
        if mcp.get("gatewayTargetState") in (None, "ready"):
            return "ready"
        return "unknown"
    if mcp.get("gatewayIdentifier"):
        return "unknown"
    if mcp.get("endpoint") or mcp.get("upstreamEndpoint"):
        return "not_applicable"
    return "unknown"


def _overlap_map() -> dict:
    """record_id → 최신 OverlapRecord. 조회 실패는 빈 dict로 degrade해요.

    중복검토는 부가 신호예요. 여기서 예외를 올리면 신호 장애가 인벤토리 조회 자체를 500으로
    만들어 관리자가 아무것도 못 봐요(자산상세 trust 합성이 `trust=None`으로 degrade하는 것과
    같은 규칙).
    """
    try:
        return get_gov_store().all_overlaps()
    except Exception:
        _log.warning("중복검토 배치 조회에 실패했어요 — 미검토로 표시해요.", exc_info=True)
        return {}


def _as_score(value) -> int | None:
    """후보 점수를 int로 정규화해요. 점수로 볼 수 없으면 None.

    **`isinstance(value, int)`로 거르면 프로덕션에서만 깨져요.** `DynamoGovStore`는 boto3
    resource 인터페이스라 저장된 숫자를 `decimal.Decimal`로 복원하고, `Decimal`은 `int`의
    인스턴스가 아니에요. 그러면 모든 후보가 걸러져 band가 `None`이 되고, 화면은 그걸
    "중복 없음"(초록)으로 그려요 — 검토 결과 High 중복인 자산이 안전으로 위장돼요.
    로컬 JSON 스토어는 int를 그대로 돌려주니 테스트는 전부 초록불이에요.

    `bool`은 `int` 서브클래스라 명시적으로 거부해요 — 점수 자리에 True가 오면 1점으로
    조용히 통과해요.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, Decimal):
        # Decimal은 int()가 소수부를 버려요. 점수는 0~100 정수 계약이라 무손실이어야 해요.
        return int(value) if value == value.to_integral_value() else None
    return None


def _gateway_target_quota() -> dict:
    """Read live target count and applied quota; observation failure is unknown."""
    sources = {
        "current": "ListGatewayTargets",
        "quota": "ServiceQuotas.GetServiceQuota",
    }

    def _unknown(reason: str) -> dict:
        return {
            "status": "unknown",
            "current": None,
            "quota": None,
            "usage_ratio": None,
            "threshold_ratio": 0.7,
            "threshold_reached": None,
            "reason": reason,
            "sources": sources,
        }

    config = load_config()
    gateway_id = (
        config.m2_oauth_gateway_id
        or config.deploy_gateway_id
        or ""
    )
    if not gateway_id:
        return _unknown("gateway identifier is not configured")
    try:
        service = get_deploy_service()
        probe = getattr(service.port, "observe_gateway_target_quota", None)
        if probe is None:
            return _unknown("Gateway target quota probe is not wired")
        observed = probe(gateway_id)
    except Exception as exc:
        return _unknown(f"{type(exc).__name__}: {exc}")
    if not isinstance(observed, dict) or observed.get("status") not in {
        "ok",
        "unknown",
    }:
        return _unknown("Gateway target quota probe returned an invalid result")
    return observed


def _top_candidate(overlap) -> dict | None:
    """가장 점수가 높은 후보 하나(점수는 int로 정규화). 후보가 없으면 None.

    저장은 이미 점수 내림차순이지만(`overlap_review._rank`), 여기서 다시 최대를 골라요 —
    저장 순서에 의존하면 옛 레코드나 다른 경로가 만든 순서에 조용히 흔들려요.

    반환하는 dict의 `score`는 항상 `int`예요. Decimal을 그대로 응답에 실으면 JSON 직렬화
    단계에서 터지거나 클라이언트가 다른 타입을 받아요.
    """
    best: dict | None = None
    best_score = -1
    for candidate in overlap.candidates or []:
        if not isinstance(candidate, dict):
            continue
        score = _as_score(candidate.get("score"))
        if score is None:
            continue
        if score > best_score:
            best, best_score = {**candidate, "score": score}, score
    return best


@router.get(
    "/api/governance/inventory",
    dependencies=[Depends(require_role(*CONSOLE_ROLES))],
)
def inventory():
    """자산 인벤토리 집계 — 소유·상태·중복 축. 읽기 전용(관측이지 결정이 아니에요)."""
    rows = _queue_annotations()
    overlaps = _overlap_map()

    assets: list[dict] = []
    candidates: list[dict] = []
    by_type: dict[str, int] = {}
    unscanned = 0
    overlap_high = 0
    gateway_target_missing = 0
    authorization_counts: dict[str, int] = {}
    config = load_config()

    for row in rows:
        record_id = row["record_id"]
        overlap = overlaps.get(record_id)
        top = _top_candidate(overlap) if overlap is not None else None

        if overlap is None:
            state, band, top_score = "not_reviewed", None, None
        elif overlap.status == "failed":
            state, band, top_score = "failed", None, None
        elif overlap.status != "done":
            state, band, top_score = "reviewing", None, None
        else:
            state = "reviewed"
            band = overlap.top_band if top else None
            top_score = top.get("score") if top else None

        if band == "high":
            overlap_high += 1
        if row["risk"] is None:
            unscanned += 1

        asset_type = row.get("asset_type") or ""
        by_type[asset_type] = by_type.get(asset_type, 0) + 1
        enforcement, enforcement_source = _authorization_enforcement(row, config)
        gateway_state = _gateway_target_state(row)
        if gateway_state == "unknown":
            gateway_target_missing += 1
        authorization_counts[enforcement] = (
            authorization_counts.get(enforcement, 0) + 1
        )

        assets.append({
            "record_id": record_id,
            "name": row["name"],
            "asset_type": asset_type,
            "owner_team": row.get("owner_team", ""),
            "owner_user": row.get("owner_user", ""),
            "model": row.get("model", ""),
            "version": row.get("version", ""),
            "status": row["status"],
            "risk": row["risk"],
            "tier": row["tier"],
            "overlap_state": state,
            "overlap_band": band,
            "overlap_top_score": top_score,
            "authorization_enforcement": enforcement,
            "authorization_enforcement_source": enforcement_source,
            # CA-32 ②: Gateway target 이 없는(=호출 불가) 자산을 드러내요.
            "gateway_target_state": gateway_state,
        })

        if state == "reviewed" and top is not None:
            candidates.append({
                "record_id": record_id,
                "name": row["name"],
                "candidate_record_id": top.get("record_id", ""),
                "candidate_name": top.get("name", ""),
                "score": top.get("score", 0),
                "band": top.get("band", "low"),
                "reasons": list(top.get("reasons", []) or []),
            })

    # 점수 내림차순, 동점은 record_id로 안정 정렬 — 같은 데이터면 같은 순서여야 화면이
    # 새로고침마다 흔들리지 않아요.
    candidates.sort(key=lambda c: (-c["score"], c["record_id"]))

    return {
        "kpi": {
            "total": len(assets),
            "by_type": by_type,
            "unscanned": unscanned,
            "overlap_high": overlap_high,
            # 등록은 됐는데 Gateway target 이 확정되지 않은 자산 수 (CA-32 고아 후보).
            "gateway_target_missing": gateway_target_missing,
        },
        "assets": assets,
        "overlap_candidates": candidates[:_MAX_CARD_CANDIDATES],
        "measurement": {axis: "not_implemented" for axis in _MEASUREMENT_AXES},
        "authorization": {
            "m2_gateway_mode": (
                config.m2_oauth_gateway_mode.lower()
                if config.m2_oauth_gateway_mode
                else "unknown"
            ),
            "source": (
                "runtime_configuration"
                if config.m2_oauth_gateway_mode
                else "unobserved"
            ),
            "counts": authorization_counts,
        },
        "gateway_target_quota": _gateway_target_quota(),
    }
