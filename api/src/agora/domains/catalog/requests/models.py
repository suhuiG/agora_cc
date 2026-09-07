"""PublishRequest — 요청 로그 1건. 동기 퍼블리시와 배포 job을 한 목록으로 합쳐요.

to_dict/from_dict로 DynamoDB(DynamoRequestLog)와 인메모리 fake를 왕복해요.
status_from_phase가 배포 job의 DeployPhase를 요청 status로 파생해요(폴러가 사용).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from threading import Lock

from ...runtime.deploy.models import DeployPhase


_timestamp_lock = Lock()
_last_request_timestamp: datetime | None = None


def request_timestamp() -> str:
    """Return a process-monotonic, microsecond UTC timestamp for new requests."""
    global _last_request_timestamp
    with _timestamp_lock:
        now = datetime.now(timezone.utc)
        if _last_request_timestamp is not None and now <= _last_request_timestamp:
            now = _last_request_timestamp + timedelta(microseconds=1)
        _last_request_timestamp = now
    return now.isoformat(timespec="microseconds").replace("+00:00", "Z")


class RequestKind(str, Enum):
    """요청 종류. 배포·권한 신청·동기 게시를 한 목록에 합쳐요."""
    DEPLOY_MCP = "deploy-mcp"
    DEPLOY_AGENT = "deploy-agent"
    TOOL_REQUEST = "tool-request"
    BUNDLE_DEPLOY = "bundle-deploy"
    SKILL = "skill"
    MCP_CONNECT = "mcp-connect"
    AGENT_JSON = "agent-json"
    AGENT_DOMAIN = "agent-domain"


class RequestStatus(str, Enum):
    """요청 진행 상태. pending/running은 진행 중, succeeded/failed는 종료."""
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass
class PublishRequest:
    """요청 로그 1건. 배포형은 job_id로 DeployJob과 연결돼요."""
    request_id: str
    principal: str
    kind: RequestKind
    status: RequestStatus
    title: str
    created_at: str = ""
    updated_at: str = ""
    record_id: str | None = None
    job_id: str | None = None
    error: dict | None = None
    # IH-77: 배포 진행을 '나의 요청'에서 보여주려면 job phase 가 요청 로그에 있어야 해요.
    # 화면이 배포 job 상태를 직접 조회하면 그 조회가 job 을 **전진**시켜서(IH-80) 이중
    # actor 문제를 다시 만들어요. 그래서 job 이 전진할 때 서버가 여기에 복사해둬요.
    phase: str | None = None
    phase_detail: str | None = None

    def to_dict(self) -> dict:
        return {
            "request_id": self.request_id, "principal": self.principal,
            "kind": self.kind.value, "status": self.status.value,
            "title": self.title,
            "created_at": self.created_at, "updated_at": self.updated_at,
            "record_id": self.record_id, "job_id": self.job_id,
            "error": self.error,
            "phase": self.phase, "phase_detail": self.phase_detail,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "PublishRequest":
        return cls(
            request_id=d["request_id"], principal=d["principal"],
            kind=RequestKind(d["kind"]), status=RequestStatus(d["status"]),
            title=d.get("title", ""),
            created_at=d.get("created_at", ""), updated_at=d.get("updated_at", ""),
            record_id=d.get("record_id"), job_id=d.get("job_id"),
            phase=d.get("phase"), phase_detail=d.get("phase_detail"),
            error=d.get("error"),
        )


@dataclass(frozen=True)
class RequestPage:
    """principal별 요청 목록 한 페이지."""

    items: list[PublishRequest]
    total_count: int
    next_cursor: str | None


_RUNNING_PHASES = (
    DeployPhase.BUILDING, DeployPhase.REGISTERING_TARGET, DeployPhase.DEPLOYING,
    # 프로비저닝·검증 중인 배포가 '나의 요청'에서 실패로 보이면 안 돼요.
    DeployPhase.PROVISIONING, DeployPhase.VERIFYING,
)


def status_from_phase(phase: DeployPhase) -> RequestStatus:
    """배포 job phase → 요청 status 파생. 폴러·라우터가 공용으로 써요."""
    if phase is DeployPhase.QUEUED:
        return RequestStatus.PENDING
    if phase in _RUNNING_PHASES:
        return RequestStatus.RUNNING
    if phase is DeployPhase.READY:
        return RequestStatus.SUCCEEDED
    return RequestStatus.FAILED


def status_from_record_status(rec_status) -> RequestStatus:
    """레코드 승인 상태(RecordStatus) → 요청 로그 status.

    등록 API 성공이 곧 '게시완료'가 아니에요: 승인 게이트(스캔 → 관리자 승인)가 남아
    PENDING_APPROVAL이면 아직 진행 중(running)으로 표시해야 My Requests에 '게시완료'로
    오표시되지 않아요. APPROVED만 succeeded, REJECTED는 failed.
    """
    s = str(getattr(rec_status, "value", rec_status) or "").upper()
    if s == "APPROVED":
        return RequestStatus.SUCCEEDED
    if s == "REJECTED":
        return RequestStatus.FAILED
    if s == "PENDING_APPROVAL":
        return RequestStatus.RUNNING
    return RequestStatus.PENDING
