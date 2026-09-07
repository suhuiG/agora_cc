"""원장 변경 뒤 공유 Gateway 정책 재프로비저닝 결과를 강제해요."""

from __future__ import annotations

from collections.abc import Callable, Mapping

# ALB idle timeout은 보통 60초예요. 정책 활성화가 그보다 길어질 수 있으므로 관리자 HTTP
# 요청은 약 8초의 폴링만 쓰고 즉시 `provisioning`을 돌려줘요. MCP 배포 job은 이 상수를
# 쓰지 않고 provisioner 기본 60폴을 유지해요.
HTTP_ACTIVE_MAX_POLLS = 8


class SharedPolicyProvisioningFailed(RuntimeError):
    """원장 변경은 끝났지만 공유 정책을 그 상태에 맞추지 못했어요."""

    def __init__(self, report: dict) -> None:
        self.report = report
        reason = str(report.get("reason") or report.get("verdict") or "unknown")
        super().__init__(reason)


def require_shared_policy_provisioned(
    provision: Callable[..., object],
) -> dict:
    """HTTP 예산 안에 프로비저닝이 명시적으로 성공한 경우만 보고서를 돌려줘요."""

    try:
        raw_report = provision(active_max_polls=HTTP_ACTIVE_MAX_POLLS)
    except Exception as exc:
        raise SharedPolicyProvisioningFailed({
            "ok": False,
            "verdict": "unknown",
            "reason": f"{type(exc).__name__}: {exc}",
        }) from exc
    if not isinstance(raw_report, Mapping):
        raise SharedPolicyProvisioningFailed({
            "ok": False,
            "verdict": "unknown",
            "reason": (
                "공유 정책 provisioner가 구조화된 결과를 돌려주지 않았어요: "
                f"{type(raw_report).__name__}"
            ),
        })
    report = dict(raw_report)
    if report.get("ok") is not True or report.get("warnings"):
        raise SharedPolicyProvisioningFailed(report)
    return report
