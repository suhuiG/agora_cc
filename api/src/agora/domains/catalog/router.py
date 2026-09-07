"""카탈로그 도메인 라우터 — 발견·상세·다운로드·퍼블리시·소스 스토어.

오너: 카탈로그 도메인. SoT(AgoraCatalog) + source store 를 소유해요.
다른 도메인은 이 라우터가 노출하는 API / shared.deps 접근자로만 카탈로그에 접근해요.
"""

from __future__ import annotations

import io
import json
import logging
import tarfile
from copy import deepcopy

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ...shared.deps import (
    get_asset_responsibility_port,
    get_aux,
    get_current_principal,
    get_registry,
    get_registry_id,
    get_source_store,
    get_stats_store,
    get_trust_summary_port,
)
from ...shared.governance import RegistrationTrigger
from ...shared.slug import slugify
from .install import INSTALL_TARGETS  # noqa: F401 (드롭다운 소스 레지스트리)
from .install import OS as InstallOS
from .install import get_target, targets_supporting
from .mcp import (
    McpDeployModeRemoved, McpEndpointError, McpHealthError, McpProtocolError,
    McpRegistration, register_mcp,
)
from .registration_hook import run_registration_hook
from .registry.models import DescriptorType
from .schemas import (
    AgentConnectTestRequest,
    AgentRegisterResponse,
    AgentRegistration,
    AssetCard,
    AssetDetail,
    CatalogPage,
    CurationRequest,
    DownloadResponse,
    GroupedTopDownloadsResponse,
    McpConnectTestRequest,
    McpRegisterResponse,
    PublishFinalizeRequest,
    PublishInitRequest,
    PublishRequest,
    PublishResponse,
    RequestLogItem,
    RequestLogPage,
    ResponsibilityChangeOut,
    ResponsibilityContactsOut,
    ResponsibilityHistoryOut,
    ResponsibilityStatusOut,
    ResponsibilityUpdateRequest,
    SearchResult,
    TopDownloadEntry,
    TopDownloadGroup,
    ViewResponse,
    VisibilityRequest,
)
from .sourcestore import (
    FileSpec,
    IncompleteUpload,
    SourceStoreError,
    VersionAlreadyExists,
    VersionNotFound,
    validate_asset_id,
)
from .sourcestore.audit import AuditContext
from .sourcestore.bindings import get_binding
from .sourcestore.semver import is_valid_semver, parse_semver

router = APIRouter(tags=["catalog"])
_log = logging.getLogger(__name__)

# asset_type → 카탈로그 레코드 타입(DescriptorType) 매핑
_ASSET_TYPE_TO_DESCRIPTOR = {
    "skill": DescriptorType.SKILL,
    "mcp": DescriptorType.MCP,
    "agent": DescriptorType.AGENT,
}


# ── 헬퍼 ─────────────────────────────────────────────────────────────
def mcp_proto_fetch():
    """fetch_mcp_tools를 호출시점에 모듈 속성으로 되돌려줘요.

    테스트가 `protocol.fetch_mcp_tools`를 monkeypatch 하면 그 값이 반영돼야 하거든요
    (def-time 바인딩이면 패치가 안 먹어요).
    """
    from .mcp import protocol as _p
    return _p.fetch_mcp_tools


def _public_source_prefix(descriptor_type: DescriptorType, source_prefix: str) -> str:
    """MCP의 내부 소스 좌표는 사용자 응답에서 숨겨요."""
    return "" if descriptor_type is DescriptorType.MCP else source_prefix


def _public_descriptors(rec) -> dict:
    descriptors = deepcopy(rec.descriptors)
    if rec.descriptor_type is DescriptorType.MCP:
        node = descriptors.get("mcp")
        if isinstance(node, dict):
            node.pop("sourcePrefix", None)
    if rec.descriptor_type is DescriptorType.AGENT:
        # IA-89 ①: 배포형 agent 의 Runtime ARN 은 응답에서 숨겨요. `get_asset` 에는 소유자
        # 검사가 없어서, 노출하면 자산 상세를 볼 수 있는 아무 사용자가 ARN 을 얻어 Runtime
        # 인바운드로 직접 POST 할 수 있어요(IA-79 「인가 우회」체인 ②). ARN 은 두 곳에 실려요
        # — `agent.runtimeArn` 과 `agent.executionBinding.runtimeArn`(같은 값). 둘 다 지워야
        # 노출이 막혀요. 배포형 여부는 `AssetDetail.runtime_deployed` boolean 으로 대체 노출해요.
        node = descriptors.get("agent")
        if isinstance(node, dict):
            node.pop("runtimeArn", None)
            binding = node.get("executionBinding")
            if isinstance(binding, dict):
                binding.pop("runtimeArn", None)
    return descriptors


def _is_runtime_deployed(rec) -> bool:
    """배포형(RUNTIME) agent 인지 — 원본 레코드에서 Runtime ARN 존재로 판정해요.

    _public_descriptors 가 ARN 을 지운 뒤라면 판정할 수 없으니, 반드시 필터 전 원본
    (rec.descriptors)을 봐요. runtimeArn 이 없는 agent 는 False 예요.
    """
    if rec.descriptor_type is not DescriptorType.AGENT:
        return False
    node = (rec.descriptors or {}).get("agent")
    if not isinstance(node, dict):
        return False
    arn = node.get("runtimeArn")
    return isinstance(arn, str) and bool(arn)


def _to_asset_detail(rec, meta, downloads: int = 0) -> AssetDetail:
    """RegistryRecord + aux meta를 AssetDetail 응답으로 변환해요(get/patch 공용).

    downloads: 호출자가 get_stats_store().get_download_count()로 조회한 값.
               실패 시 0을 넘겨요(spec §7: 상세 조회 중 카운트 실패 → downloads=0, 200).
    """
    from .registry.models import endpoint_of_descriptors
    # trust는 선택적 enrichment예요. 신뢰신호 조회 실패(governance 스토어 장애 등)가
    # 자산 상세 조회 전체를 500으로 만들지 않도록 경계에서 삼키고 trust=None으로 degrade해요.
    try:
        trust = get_trust_summary_port().summarize(rec.record_id).to_dict()
    except Exception:
        _log.warning("trust summary 조회 실패 — trust=None으로 응답해요.",
                     exc_info=True)
        trust = None
    owner_email = get_aux().get_ext(rec.record_id).get("owner_email", "")
    return AssetDetail(
        record_id=rec.record_id, name=rec.name,
        descriptor_type=rec.descriptor_type.value, version=rec.version,
        status=rec.status.value, description=rec.description,
        owner_team=rec.owner_team, owner_user=rec.owner_user,
        owner_email=owner_email,
        owner_contact=rec.owner_contact,
        escalation_contact=rec.escalation_contact,
        tags=list(rec.tags), category=rec.category, descriptors=_public_descriptors(rec),
        views=meta.views, downloads=downloads,
        source_prefix=_public_source_prefix(rec.descriptor_type, rec.source_prefix),
        # 좌표(sourcePrefix)는 MCP에 대해 숨기지만, 배포형 여부는 boolean으로 노출해요(CA-16).
        source_managed=bool(rec.source_prefix),
        # IA-89 ①: Runtime ARN 은 숨기되(위 _public_descriptors), 배포형 여부는 원본 레코드에서
        # 판정해 boolean 으로 노출해요. 필터 전 원본을 읽어야 하니 rec.descriptors 를 직접 봐요.
        runtime_deployed=_is_runtime_deployed(rec),
        endpoint=endpoint_of_descriptors(rec.descriptors) or None,
        trust=trust,
    )


def _principal(request) -> str:
    return AuditContext.from_request(request).principal()


def _owner_team(request, requested: str) -> str:
    """등록에 기록할 소유 팀 — 검증된 신원의 team이 있으면 그걸, 없으면 요청 값을 써요.

    신원의 team을 우선하는 이유: 소유 팀은 감사·에스컬레이션의 기준이라, 등록자가 남의 팀
    이름을 적어 넣을 수 있으면 소유 추적이 무의미해져요.

    다만 **강제하지는 않아요.** 인사 IdP가 `custom:team`을 채우지 않는 환경이 정상이고, 그때
    team을 빈 값으로 덮어쓰면 지금 동작하는 자유입력 등록이 전부 소유 팀 없는 자산이 돼요.
    값이 있을 때만 신뢰하고, 없으면 요청 값을 그대로 존중해요.
    """
    from ..identity.context import current_principal

    try:
        team = (current_principal(request).team or "").strip()
    except Exception:
        # 인증 컨텍스트를 못 읽는 경로(테스트 헤더 모드 등)에서는 요청 값을 써요.
        return requested
    return team or requested


def _store_owner_email(record_id: str, request: Request) -> None:
    """검증된 principal의 표시용 email을 Aux에 저장해 신규 자산부터 점진 적용해요."""
    from ..identity.context import current_principal

    try:
        email = (current_principal(request).email or "").strip()
        if email:
            get_aux().set_meta(record_id, owner_email=email)
    except Exception:
        # email은 표시 보강 정보라 저장 실패가 자산 등록을 되돌리면 안 돼요.
        _log.warning(
            "자산 담당자 email 저장에 실패했어요.",
            extra={"record_id": record_id},
            exc_info=True,
        )


def _seed_mcp_tool_drift(record_id: str) -> None:
    """등록·재등록 직후 도구 드리프트 원장을 descriptor 기준으로 맞춰요 (LC-03).

    네트워크를 쓰지 않아요 — 방금 등록에 쓴 tool 목록을 원장에 옮겨 적을 뿐이에요.
    원장이 이미 있으면(재등록) 통째로 덮지 않고 **대조**해요: 같은 자산 이름으로 다시
    등록해도 관리자가 남긴 상태가 살아 있어야 하거든요(IH-22).

    실패가 등록을 되돌리면 안 돼요 — 원장은 관측 산출물이고, 비어 있으면 다음
    "다시 읽기"나 주기 폴러가 채워요.
    """
    try:
        from ...shared.deps import get_mcp_drift_service

        get_mcp_drift_service().seed_from_record(record_id)
    except Exception:
        _log.warning(
            "MCP 도구 드리프트 원장 seed에 실패했어요.",
            extra={"record_id": record_id},
            exc_info=True,
        )


def _owner_contact(request: Request) -> str:
    """1차 담당자(운영 연락처) — 검증된 principal의 email에서 **서버가** 파생해요 (CA-29).

    클라이언트 입력을 받지 않아요(§11.1·§11.6의 owner 파생 규칙과 같은 취급). 화면에는
    읽기 전용으로만 보여줘요.

    승인 계약이 읽는 필드는 `owner_contact`인데(`catalog/responsibility.py`) 등록 경로가
    표시용 `owner_email`만 채워서, **어떤 자산도 등록만으로는 자동승인될 수 없었어요**
    (CA-28 ②). 두 필드를 같은 값으로 채워 그 간극을 닫아요.

    email을 못 읽는 환경(테스트 헤더 모드, email claim 없는 IdP)에서는 빈 문자열이에요 —
    등록을 막지는 않고, 승인 시점에 `responsibility_incomplete`로 관측돼요(ADR-0069 §대안).
    """
    from ..identity.context import current_principal

    try:
        return (current_principal(request).email or "").strip().lower()
    except Exception:
        return ""


def _require_responsibility_contacts(
    owner_contact: str, escalation_contact: str, *, trigger: str
) -> None:
    """등록 시점에 담당자 계약을 검증해요 (CA-29 ③ · ADR-0069).

    판정은 승인 게이트가 쓰는 `responsibility_status()` **그 함수**로 해요. 등록용 규칙을
    따로 쓰면 두 시점이 조용히 어긋나서, 등록은 통과하고 승인만 막히는 지금 상태(CA-28)가
    다시 만들어져요.

    막는 것은 **등록자가 고칠 수 있는 것**(에스컬레이션 연락처)뿐이에요. `owner_contact`는
    서버가 principal에서 파생하니 사용자가 손댈 수 없고, 비어 있으면 경고만 남겨요.
    """
    from .responsibility import responsibility_status

    status = responsibility_status("", owner_contact, escalation_contact)
    reasons = set(status.blocking_reasons)
    messages = {
        "escalation_contact_required": (
            "2차 담당자(에스컬레이션)를 지정해 주세요 — 회원 검색으로 본인이 아닌 "
            "구성원 한 명을 고르면 돼요."
        ),
        "escalation_contact_invalid": (
            "2차 담당자는 email 주소여야 해요 — 온보딩된 회원을 검색해 고르면 "
            "email이 자동으로 채워져요."
        ),
        "distinct_escalation_contact_required": (
            "2차 담당자는 등록자 본인과 달라야 해요 — 소유자가 부재일 때 연락할 "
            "다른 구성원을 골라 주세요."
        ),
    }
    for reason, message in messages.items():
        if reason in reasons:
            raise HTTPException(422, message)
    if "owner_contact_required" in reasons or "owner_contact_invalid" in reasons:
        # 등록은 막지 않아요(서버 파생값이라 등록자가 고칠 수 없어요). 승인 시점에
        # `responsibility_incomplete`로 원장·화면에 드러나요(CA-28).
        _log.warning(
            "1차 담당자 연락처를 principal에서 파생하지 못했어요 — 승인 전 보완이 필요해요.",
            extra={"trigger": trigger, "owner_contact": owner_contact},
        )


def _note_missing_escalation(record_id: str, contact: str, trigger: str) -> None:
    """2차 담당자 연락처가 계약을 못 채우면 운영 보완 대상으로 알려요.

    ADR-0064는 강제 시점을 승인 직전으로 뒀고, ADR-0069가 등록 시점 거절을 더했어요.
    그래도 이 경고는 남겨요 — 등록 시점 검증을 우회한 경로(API 직호출 이전에 만들어진
    자산, 관리자 대행 등록)가 승인 전에 보완돼야 할 대상으로 로그에 남아야 하거든요.
    빈 값만 보던 예전 판정은 `'admin'` 같은 비-email을 통과시켰어요(CA-29).
    """
    from .responsibility import responsibility_status

    status = responsibility_status("", "", contact or "")
    bad = [r for r in status.blocking_reasons if r.startswith("escalation_contact")]
    if not bad:
        return
    _log.warning(
        "2차 담당자 연락처가 승인 계약을 채우지 못해요 — 승인 전 보완이 필요해요.",
        extra={"record_id": record_id, "trigger": trigger, "reasons": bad},
    )


def _duplicate_registration_error(name: str, version: str) -> HTTPException:
    """같은 이름·버전 재등록을 409 + 행동 가능한 안내로 바꿔요 (CA-33).

    예전에는 `ConflictException`이 처리되지 않고 전파돼 500 + 스택트레이스였어요. 500은
    서버 결함처럼 보이는데 실제로는 **입력 문제**(버전을 올리거나 이름을 바꾸면 되는 것)라,
    사용자가 다음에 무엇을 할지 알 수 없었어요.
    """
    label = f"{name} {version}".strip()
    return HTTPException(409, {
        "message": (
            f"같은 이름의 {label}이(가) 이미 있어요 — 버전을 올리거나 이름을 "
            "바꿔주세요."
        ),
        "remediation": "버전을 올려서(예: 1.0.1) 다시 등록하거나, 다른 이름을 쓰세요.",
        "name": name,
        "version": version,
    })


def _status_or(hook_status: str, fallback):
    """등록 hook이 확정한 상태를 RecordStatus로. 알 수 없는 값이면 create_record 상태 유지.

    동기 등록 경로에서 빈 status는 `run_registration_hook`이 이미 503으로 막아요 —
    여기까지 빈 값이 오지 않아요. fallback은 hook이 Registry enum에 없는 상태 문자열을
    돌려주는 경우(스키마 확장 등)를 위한 방어예요. 응답에 거짓 상태를 싣지 않고 실제로
    관측된 생성 직후 상태를 내려요.
    """
    from .registry.models import RecordStatus

    if not hook_status:
        return fallback
    try:
        return RecordStatus(hook_status)
    except ValueError:
        return fallback


def _log_request(kind, title, principal, *, job_id=None, record_id=None,
                 status=None, error=None, request_id=None):
    """요청 로그 1건을 write하고 request_id를 돌려줘요.

    동기 경로는 status를 명시(succeeded/failed), 배포 경로는 기본 pending.
    로그 write 실패는 퍼블리시를 막지 않아요(비차단) — 진행/성공이 우선.
    """
    import uuid
    from .requests.models import PublishRequest, RequestStatus, request_timestamp
    from ...shared.deps import get_request_log
    now = request_timestamp()
    req = PublishRequest(
        request_id=request_id or uuid.uuid4().hex,
        principal=principal, kind=kind,
        status=status or RequestStatus.PENDING, title=title,
        created_at=now, updated_at=now, job_id=job_id, record_id=record_id,
        error=error,
    )
    try:
        get_request_log().put(req)
    except Exception:
        pass
    return req.request_id


def _reclaim_failed_registration(record_id: str, *, actor: str) -> bool:
    """실패한 등록의 레코드를 회수하고, **실제로 사라졌는지 독립적으로 확인**해요 (CA-32).

    회수 자체는 IH-81 이 만든 `PurgeService` 를 재사용해요 — 레코드만 지우면 인가 원장·
    Cedar permit·요청 로그가 남아 또 다른 고아가 되니까요.

    purge 가 돌려주는 보고를 성공 근거로 쓰지 않아요(ADR-0037 §4: 판정의 기대값은 판정
    대상이 아닌 다른 소유자에게서 와야 해요). Registry 에 다시 물어 `RecordNotFound` 를
    관측했을 때만 True 예요. 관측하지 못하면 False — "되돌렸다"고 말하지 않아요.
    """
    from ...shared.deps import get_purge_service
    from .registry.models import RecordNotFound

    try:
        get_purge_service().purge(record_id, actor=actor)
    except Exception:
        _log.exception("failed registration reclamation errored: %s", record_id)
    try:
        get_registry().get_record(get_registry_id(), record_id)
    except RecordNotFound:
        return True
    except Exception:
        _log.warning(
            "회수 결과를 관측하지 못했어요 — 회수됐다고 표시하지 않아요.",
            extra={"record_id": record_id}, exc_info=True,
        )
        return False
    return False


def _log_failed_request(kind, title, principal, *, reason: str,
                        remediation: str = "", record_id: str | None = None) -> None:
    """실패한 동기 등록도 "나의 요청"에 남겨요 (CA-34 · ADR-0070).

    연결형 등록이 마지막 단계에서 실패하면 예전에는 요청 로그가 아예 안 생겼어요 — 등록자는
    자산도, 실패 사실도, 이유도 그 화면에서 볼 수 없었어요(관리자만 승인 큐에서 봤어요).
    실패를 기록하지 않는 건 "성공만 기록"이라는 조용한 손실이에요.
    """
    from .requests.models import RequestStatus

    _log_request(
        kind, title, principal, record_id=record_id,
        status=RequestStatus.FAILED,
        error={"reason": reason, "remediation": remediation} if remediation
        else {"reason": reason},
    )


def _count_download(record_id: str | None) -> None:
    """다운로드를 1 기록해요. 실패·미상은 조용히 넘어가요(본 응답을 막지 않음)."""
    if not record_id:
        return
    try:
        rec = get_registry().get_record(get_registry_id(), record_id)
        get_stats_store().increment_download(
            record_id, name=rec.name, descriptor_type=rec.descriptor_type.value,
            owner_user=rec.owner_user)
    except Exception:
        _log.exception("download count failed: %s", record_id)


def _require_owner(record_id: str, principal: str):
    """레코드 조회 + 소유권 검증(§13). 없으면 404, 소유자 아니면 403."""
    from .registry.models import RecordNotFound
    try:
        rec = get_registry().get_record(get_registry_id(), record_id)
    except RecordNotFound:
        raise HTTPException(404, "Asset not found")
    if rec.owner_user != principal:
        raise HTTPException(403, "본인이 등록한 자산만 수정·삭제할 수 있어요.")
    return rec


def _build_descriptors(dtype: DescriptorType, req: PublishRequest) -> dict:
    if dtype is DescriptorType.SKILL:
        if not req.skill_markdown:
            raise HTTPException(422, "Skill 타입은 skill_markdown이 필요해요.")
        return {"skill": {"markdown": req.skill_markdown}}
    elif dtype is DescriptorType.MCP:
        raise HTTPException(
            400, "MCP는 /api/mcp/register 로 등록해요 (중앙 호스팅).")
    elif dtype is DescriptorType.AGENT:
        if not req.agent_endpoint:
            raise HTTPException(422, "Agent 타입은 agent_endpoint가 필요해요.")
        # endpoint를 top-level에 둬 aws_mapping이 A2A agentCard(url=endpoint)로 조립하게 해요.
        # agentCard는 표시용 원본(무손실 왕복은 Aux). aws 변환은 aws_mapping이 담당.
        return {
            "agent": {
                "endpoint": req.agent_endpoint,
                "agentCard": {
                    "name": req.name,
                    "description": req.description,
                    "version": req.version,
                    "capabilities": req.agent_capabilities,
                    "endpoint": req.agent_endpoint,
                },
            }
        }
    else:
        raise HTTPException(400, "지원하지 않는 타입이에요.")


# ── 다운로드 집계 + 톱5 ──────────────────────────────────────────────

@router.get("/api/catalog/top-downloads", response_model=GroupedTopDownloadsResponse)
def get_top_downloads(force: bool = Query(False)):
    """자산 타입별 인기 다운로드 톱5를 반환해요 — 카드 하나당 한 그룹.

    타입마다 **자기 안에서** 상위 5개를 골라요(전체 톱5를 쪼개는 게 아니에요).
    groups는 항상 GROUP_ORDER 순서·길이라, 다운로드 0건인 타입도 빈 그룹으로
    들어와요(화면에서 빈 카드로 표시).

    UI가 열릴 때마다 호출하는 경로라 **매번 실집계**해요(1시간 TTL에 갇히지 않음).
    force는 새로고침 버튼이 쓰는데, 이 경로는 이미 실집계라 동작이 같아요 —
    계약 대칭과 의도 표현을 위해 받아요.

    정적 경로라 다른 카탈로그 라우트와 충돌하지 않아요.
    """
    result = get_stats_store().top_downloads_grouped(force=force)
    aux = get_aux()
    return GroupedTopDownloadsResponse(
        computed_at=result.computed_at,
        groups=[
            TopDownloadGroup(
                descriptor_type=group.descriptor_type,
                items=[
                    TopDownloadEntry(
                        record_id=entry.record_id,
                        name=entry.name,
                        descriptor_type=entry.descriptor_type,
                        downloads=entry.downloads,
                        owner_user=entry.owner_user,
                        owner_email=aux.get_ext(entry.record_id).get(
                            "owner_email",
                            "",
                        ),
                    )
                    for entry in group.items
                ],
            )
            for group in result.groups
        ],
    )


# ── 발견 (Discover) ──────────────────────────────────────────────────
@router.get("/api/catalog/categories")
def list_catalog_categories():
    """등록 폼용 카테고리 목록. 미등록 사용값은 관리자 API에만 노출해요."""
    from ...shared.deps import get_gov_store

    return {"items": get_gov_store().get_categories()}


@router.get("/api/catalog", response_model=CatalogPage)
def list_catalog(
    type: str | None = Query(None, description="MCP / Agent / Agent Skills"),
    offset: int = Query(0, ge=0),
    limit: int = Query(6, ge=1, le=100),
):
    """카탈로그 목록 (APPROVED). type 필터 + offset/limit pagination.

    정렬(updated_at desc, name)을 전체에 먼저 적용한 뒤 slice해요 —
    offset pagination은 페이지 간 정렬이 고정돼야 중복·누락이 없거든요.
    """
    registry = get_registry()
    registry_id = get_registry_id()
    aux = get_aux()
    dtype = None
    if type:
        try:
            dtype = DescriptorType(type)
        except ValueError:
            raise HTTPException(400, f"Invalid type: {type}")
    # 정렬 대상 전체 확보 후 slice. max_results=100은 AWS ListRegistryRecords의
    # 상한이에요(초과 시 ValidationException). 현재 자산 규모에선 충분하고, 100을
    # 넘어가면 total이 부정확해지므로 그때 cursor pagination으로 재설계해요(spec §8).
    # 카드는 search hit + AuxStore(로컬)만으로 조립해요. 자산마다 get_record
    # (AWS GetRegistryRecord)를 부르면 N+1 왕복이라 목록이 자산 수에 비례해 느려지거든요.
    hits = registry.search([registry_id], "", descriptor_type=dtype, max_results=100)
    rows = []
    for h in hits:
        meta = aux.get_meta(h.record_id)
        owner_email = aux.get_ext(h.record_id).get("owner_email", "")
        card = AssetCard(
            record_id=h.record_id,
            name=h.name,
            descriptor_type=h.descriptor_type.value,
            version=h.version,
            status=h.status.value,
            description=h.description,
            owner_team=h.owner_team,
            owner_user=h.owner_user,
            owner_email=owner_email,
            tags=list(h.tags),
            category=h.category,
            views=meta.views,
            source_prefix=_public_source_prefix(h.descriptor_type, h.source_prefix),
            endpoint=h.endpoint or None,
        )
        rows.append((h.updated_at or "", card))
    # 최근 수정일자 내림차순(updated_at 큰 값이 먼저). 같으면 이름순. 정렬 후 slice.
    rows.sort(key=lambda x: (x[0], x[1].name), reverse=True)
    cards = [card for _, card in rows]
    total = len(cards)
    page = cards[offset:offset + limit]
    return CatalogPage(items=page, total=total, offset=offset, limit=limit)


@router.get("/api/search", response_model=list[SearchResult])
def search_assets(
    q: str = Query(..., min_length=1, description="검색 쿼리"),
    type: str | None = Query(None),
    # AWS SearchRegistryRecords는 pagination 없이 최대 20건만 지원해요.
    max_results: int = Query(10, ge=1, le=20),
):
    """키워드 검색 (APPROVED만)."""
    registry = get_registry()
    registry_id = get_registry_id()
    dtype = None
    if type:
        try:
            dtype = DescriptorType(type)
        except ValueError:
            raise HTTPException(400, f"Invalid type: {type}")
    hits = registry.search([registry_id], q, descriptor_type=dtype, max_results=max_results)
    # source_prefix는 hit에 이미 담겨 있어 get_record 추가 왕복이 필요 없어요(N+1 회피).
    results = []
    for h in hits:
        results.append(SearchResult(
            record_id=h.record_id,
            name=h.name,
            descriptor_type=h.descriptor_type.value,
            version=h.version,
            description=h.description,
            score=h.score,
            source_prefix=_public_source_prefix(h.descriptor_type, h.source_prefix),
        ))
    return results


# ── Playground (배포 완료 Agent 목록) ─────────────────────────────────

class DeployedAgent(BaseModel):
    record_id: str
    name: str
    version: str


class DeployedAgentPage(BaseModel):
    items: list[DeployedAgent]
    next_cursor: str | None = None
    incomplete_reason: str = ""


# botocore의 ListRegistryRecords nextToken shape는 최대 2,048자예요. 내부 JSON과
# base64 envelope 오버헤드를 포함해도 자기 발급 커서를 다시 받을 수 있게 4KiB로 둬요.
_PLAYGROUND_CURSOR_MAX = 4096


def _playground_cursor(query: str, registry_cursor: str) -> str:
    import base64
    import hashlib
    import json

    fingerprint = hashlib.sha256(query.casefold().encode()).hexdigest()[:16]
    payload = json.dumps(
        {"v": 1, "q": fingerprint, "t": registry_cursor},
        separators=(",", ":"),
    ).encode()
    cursor = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    if len(cursor) > _PLAYGROUND_CURSOR_MAX:
        raise HTTPException(502, "Registry returned an oversized pagination cursor")
    return cursor


def _playground_registry_cursor(cursor: str | None, query: str) -> str | None:
    import base64
    import hashlib
    import json

    if not cursor:
        return None
    try:
        if len(cursor) > _PLAYGROUND_CURSOR_MAX:
            raise ValueError
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded))
        fingerprint = hashlib.sha256(query.casefold().encode()).hexdigest()[:16]
        if (
            payload.keys() != {"v", "q", "t"}
            or payload["v"] != 1
            or payload["q"] != fingerprint
            or not isinstance(payload["t"], str)
            or not payload["t"]
        ):
            raise ValueError
        return payload["t"]
    except Exception as exc:
        raise HTTPException(400, "Invalid playground agent cursor") from exc


@router.get("/api/playground/agents", response_model=DeployedAgentPage)
def list_deployed_agents(
    name: str = Query(default="", max_length=100),
    page_size: int = Query(default=20, ge=1, le=50),
    cursor: str | None = Query(default=None, max_length=_PLAYGROUND_CURSOR_MAX),
):
    """Playground 검색 모달용 배포 Agent 페이지.

    AWS Registry search hit에 Aux descriptor의 최소 배포 투영을 병합하고, Aux에
    판정 근거가 없는 record만 권위 조회해요. 커서는 검색어에 결속된 불투명
    오프셋이고 서버 상한 안에서만 다음 페이지를 허용해요.
    """
    registry = get_registry()
    registry_id = get_registry_id()
    query = name.strip()
    registry_cursor = _playground_registry_cursor(cursor, query)
    try:
        result = registry.list_deployed_agent_hits(
            registry_id,
            name=query,
            page_size=page_size,
            cursor=registry_cursor,
        )
    except ValueError as exc:
        raise HTTPException(400, "Invalid playground agent cursor") from exc
    items = [
        DeployedAgent(record_id=h.record_id, name=h.name, version=h.version)
        for h in result.items
    ]
    next_cursor = (
        _playground_cursor(query, result.next_cursor)
        if result.next_cursor
        else None
    )
    return DeployedAgentPage(
        items=items,
        next_cursor=next_cursor,
        incomplete_reason=result.incomplete_reason,
    )


@router.get("/api/assets/{record_id}", response_model=AssetDetail)
def get_asset(record_id: str):
    """자산 상세 조회(순수 조회 — 부수효과 없음).

    조회수는 POST /api/assets/{id}/view가 담당해요. GET을 순수하게 유지해야
    프론트의 SWR 캐시·재검증(포커스·재방문)이 조회수를 중복 증가시키지 않거든요.
    다운로드 카운트 조회 실패 시 downloads=0으로 200을 반환해요(spec §7).
    """
    from .registry.models import RecordNotFound
    registry = get_registry()
    aux = get_aux()
    try:
        rec = registry.get_record(get_registry_id(), record_id)
    except RecordNotFound:
        raise HTTPException(404, "Asset not found")
    meta = aux.get_meta(record_id)
    try:
        downloads = get_stats_store().get_download_count(record_id)
    except Exception:
        downloads = 0
    return _to_asset_detail(rec, meta, downloads=downloads)


def _responsibility_out(status) -> ResponsibilityStatusOut:
    return ResponsibilityStatusOut(
        record_id=status.record_id,
        owner_contact=status.contacts.owner_contact,
        escalation_contact=status.contacts.escalation_contact,
        status=status.status,
        required_for=status.required_for,
        blocking_reasons=list(status.blocking_reasons),
        authorization_effect=status.authorization_effect,
    )


def _require_admin(request: Request):
    principal = get_current_principal(request)
    if not principal.is_admin:
        raise HTTPException(403, "책임자 변경과 감사 이력 조회는 admin 전용이에요.")
    return principal


@router.get(
    "/api/assets/{record_id}/responsibility",
    response_model=ResponsibilityStatusOut,
)
def get_asset_responsibility(record_id: str):
    """Return operational contacts and production-approval readiness."""
    from .registry.models import RecordNotFound

    try:
        return _responsibility_out(
            get_asset_responsibility_port().get(record_id)
        )
    except RecordNotFound:
        raise HTTPException(404, "Asset not found")


@router.put(
    "/api/assets/{record_id}/responsibility",
    response_model=ResponsibilityStatusOut,
)
def update_asset_responsibility(
    record_id: str,
    body: ResponsibilityUpdateRequest,
    request: Request,
):
    """Admin-only operational handoff; it never changes authorization ownership."""
    from ...shared.responsibility import ResponsibilityConflict
    from .registry.models import RecordNotFound

    principal = _require_admin(request)
    try:
        status = get_asset_responsibility_port().change(
            record_id,
            owner_contact=body.owner_contact,
            escalation_contact=body.escalation_contact,
            changed_by=principal.principal_id,
            reason=body.reason,
        )
    except RecordNotFound:
        raise HTTPException(404, "Asset not found")
    except ResponsibilityConflict:
        raise HTTPException(
            409,
            "책임자 정보가 동시에 변경됐어요. 최신 값을 조회한 뒤 다시 시도하세요.",
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    return _responsibility_out(status)


@router.get(
    "/api/assets/{record_id}/responsibility/history",
    response_model=ResponsibilityHistoryOut,
)
def list_asset_responsibility_history(record_id: str, request: Request):
    """Admin-only append-only responsibility change history."""
    from .registry.models import RecordNotFound

    _require_admin(request)
    try:
        events = get_asset_responsibility_port().history(record_id)
    except RecordNotFound:
        raise HTTPException(404, "Asset not found")
    return ResponsibilityHistoryOut(
        items=[
            ResponsibilityChangeOut(
                event_id=event.event_id,
                record_id=event.record_id,
                changed_at=event.changed_at,
                changed_by=event.changed_by,
                reason=event.reason,
                before=ResponsibilityContactsOut(
                    owner_contact=event.before.owner_contact,
                    escalation_contact=event.before.escalation_contact,
                ),
                after=ResponsibilityContactsOut(
                    owner_contact=event.after.owner_contact,
                    escalation_contact=event.after.escalation_contact,
                ),
            )
            for event in events
        ]
    )


@router.post("/api/assets/{record_id}/view", response_model=ViewResponse)
def record_view(record_id: str):
    """상세 조회 1회당 조회수를 올려요. 프론트 상세 페이지가 마운트 시 호출해요."""
    from .registry.models import RecordNotFound
    registry = get_registry()
    aux = get_aux()
    try:
        registry.get_record(get_registry_id(), record_id)
    except RecordNotFound:
        raise HTTPException(404, "Asset not found")
    views = aux.increment_views(record_id)
    return ViewResponse(record_id=record_id, views=views)


@router.post("/api/assets/{record_id}/download", response_model=DownloadResponse)
def record_download(record_id: str):
    """다운로드 1회를 기록해요. store 실패 시 500(카운트가 이 요청의 목적 — spec §7).

    레코드를 조회해 name·descriptor_type·owner_user를 얻어 stats store에 넘겨요.
    자산 없으면 404, store가 raise하면 500.
    """
    from .registry.models import RecordNotFound
    registry = get_registry()
    try:
        rec = registry.get_record(get_registry_id(), record_id)
    except RecordNotFound:
        raise HTTPException(404, "Asset not found")
    downloads = get_stats_store().increment_download(
        record_id,
        name=rec.name,
        descriptor_type=rec.descriptor_type.value,
        owner_user=rec.owner_user,
    )
    return DownloadResponse(record_id=record_id, downloads=downloads)


@router.patch("/api/assets/{record_id}", response_model=AssetDetail)
def edit_asset_curation(record_id: str, req: CurationRequest, request: Request):
    """본인 자산의 큐레이션 필드만 수정해요(§13). name/version/소스/owner는 불변."""
    registry = get_registry()
    registry_id = get_registry_id()
    aux = get_aux()
    principal = _principal(request)
    _require_owner(record_id, principal)
    tags = tuple(req.tags) if req.tags is not None else None
    registry.update_curation(
        registry_id, record_id,
        description=req.description, tags=tags,
        category=req.category, changelog=req.changelog,
    )
    # 갱신된 상세를 반환 — get_asset과 같은 _to_asset_detail 헬퍼를 쓰되,
    # increment_views는 호출하지 않음(수정은 조회 아님).
    # 다운로드 카운트 조회 실패 시 downloads=0으로 200을 반환해요(spec §7).
    rec = registry.get_record(registry_id, record_id)
    meta = aux.get_meta(record_id)
    try:
        downloads = get_stats_store().get_download_count(record_id)
    except Exception:
        downloads = 0
    return _to_asset_detail(rec, meta, downloads=downloads)


@router.delete("/api/assets/{record_id}")
def delete_asset(record_id: str, request: Request):
    """본인 자산을 완전 삭제해요(§13). 소스·버전·거버넌스·요청·번들·AWS 리소스까지 전부.

    soft delete(DEPRECATED)에서 하드 삭제로 교체됐어요. 소유권 게이트(_require_owner)를
    통과하면 PurgeService가 의존성 역순으로 모든 저장소·리소스를 best-effort 정리하고,
    단계별 결과(PurgeReport)를 응답에 실어줘요.
    """
    from ...shared.deps import get_purge_service
    principal = _principal(request)
    _require_owner(record_id, principal)        # 404(없음)/403(소유자 아님) 게이트
    report = get_purge_service().purge(record_id, actor=principal)
    if "registry" not in report.deleted:
        message = (
            "일부 정리를 완료하지 못해 자산 레코드를 보존했어요. "
            "결과를 확인한 뒤 다시 시도해 주세요."
        )
    elif report.failed:
        message = (
            "자산 레코드는 삭제했지만 일부 관련 데이터 정리에 실패했어요. "
            "결과를 확인해 주세요."
        )
    else:
        message = "자산과 관련 데이터를 완전히 삭제했어요."
    return {
        "record_id": record_id,
        "report": report.to_dict(),
        "message": message,
    }


def _request_items(reqs) -> list[RequestLogItem]:
    """요청 로그를 응답 항목으로 조립하고 심사 진행(governance)을 합성해요.

    조립을 한 곳에 모아 둬요 — 목록과 poke 두 엔드포인트가 같은 표현을 돌려줘야 하는데,
    각자 인라인으로 만들면 한쪽에만 필드가 추가되는 불일치가 생겨요.

    심사 축은 자산이 등재된 뒤(record_id 확보)에만 의미가 있어요. 조회 실패는 governance=None
    으로 degrade해요 — 심사 신호를 못 읽는 게 요청 목록 전체를 500으로 만들면 안 되니까요
    (자산 상세의 trust 조회와 같은 degrade 규약).
    """
    from concurrent.futures import ThreadPoolExecutor
    from .registry.models import RecordNotFound

    try:
        port = get_trust_summary_port()
    except Exception:
        port = None
    registry = get_registry()
    registry_id = get_registry_id()

    def enrich(r):
        governance = None
        catalog_status = None
        conversation_manager = None
        record = None
        if r.record_id:
            try:
                record = registry.get_record(registry_id, r.record_id)
                catalog_status = record.status.value
                agent = record.descriptors.get("agent")
                manager = (
                    agent.get("conversationManager", {}).get("actual")
                    if isinstance(agent, dict)
                    else None
                )
                if (
                    isinstance(manager, dict)
                    and isinstance(manager.get("name"), str)
                    and isinstance(manager.get("parameters"), dict)
                ):
                    conversation_manager = {
                        "name": manager["name"],
                        "parameters": manager["parameters"],
                    }
            except RecordNotFound:
                catalog_status = "DELETED"
            except Exception:
                _log.warning(
                    "요청 카탈로그 상태 조회 실패 — catalog_status=None으로 응답해요.",
                    extra={"record_id": r.record_id},
                    exc_info=True,
                )
            if port:
                try:
                    governance = port.summarize(
                        r.record_id,
                        record=record,
                    ).to_dict()
                except Exception:
                    _log.warning("요청 심사 진행 조회 실패 — governance=None으로 응답해요.",
                                 extra={"record_id": r.record_id}, exc_info=True)
        return RequestLogItem(
            request_id=r.request_id, kind=r.kind.value, status=r.status.value,
            title=r.title, created_at=r.created_at, updated_at=r.updated_at,
            record_id=r.record_id, job_id=r.job_id, error=r.error,
            governance=governance,
            phase=getattr(r, "phase", None),
            phase_detail=getattr(r, "phase_detail", None),
            catalog_status=catalog_status,
            conversation_manager=conversation_manager,
        )

    if not reqs:
        return []
    # Registry/거버넌스 enrich는 서로 독립이고 페이지당 최대 10건이라 병렬로 대기해
    # 프론트 N+1 제거가 서버의 직렬 N+1 지연으로 옮겨가지 않게 해요.
    with ThreadPoolExecutor(max_workers=min(10, len(reqs))) as executor:
        return list(executor.map(enrich, reqs))


@router.get("/api/requests", response_model=RequestLogPage)
def list_my_requests(request: Request, cursor: str | None = Query(default=None)):
    """내 퍼블리시 요청 이력 — 최신순 10건과 다음 페이지 cursor."""
    from ...shared.deps import get_request_log
    principal = _principal(request)
    try:
        page = get_request_log().page_by_principal(
            principal,
            limit=10,
            cursor=cursor,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return RequestLogPage(
        items=_request_items(page.items),
        total_count=page.total_count,
        next_cursor=page.next_cursor,
    )


@router.post("/api/requests/poke", response_model=RequestLogPage)
async def poke_requests(request: Request):
    """진행 중인 배포 job을 1단계 강제 전진시켜요('재요청' 버튼).

    서버측 폴러(AGORA_POLLER_ENABLED)가 꺼진 환경에서 배포 job이 QUEUED에 멈추는 걸
    사용자가 수동으로 풀 수 있게 해요. poll_once가 미종료 job 전부를 1단계 advance하고
    요청 로그 status를 job phase에서 sync해요(폴러 1회분과 동일). 갱신된 내(principal)
    요청 목록을 그대로 돌려줘 프론트가 즉시 반영해요.
    """
    import logging
    from datetime import datetime, timezone
    from ...shared.deps import get_deploy_service, get_request_log
    from ..runtime.deploy.poller import poll_once
    from .requests.models import RequestStatus, status_from_phase
    from .requests.store import sync_from_job

    def _now() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    _log = logging.getLogger(__name__)
    deploy_service = get_deploy_service()
    request_log = get_request_log()
    principal = _principal(request)

    # (1) 미종료 job을 1단계 전진 + 요청 sync (폴러 1회분).
    try:
        await poll_once(deploy_service, request_log, now=_now)
    except Exception as e:  # 한 job 실패가 응답 전체를 막지 않게 흡수.
        _log.warning("poke_requests poll_once failed: %s", e)

    # (2) 요청은 진행 중(pending/running)인데 대응 job이 이미 terminal이면,
    #     poll_once가 스킵하므로 여기서 요청 status를 job phase로 강제 sync해요.
    #     (배포는 끝났는데 요청 로그만 뒤처진 케이스 — 이 화면의 주 증상)
    for r in request_log.list_by_principal(principal):
        if r.status not in (RequestStatus.PENDING, RequestStatus.RUNNING):
            continue
        if not r.job_id:
            continue
        try:
            job = deploy_service.poll(r.job_id)
            if job is not None and status_from_phase(job.phase) != r.status:
                sync_from_job(request_log, job, now=_now)
        except Exception as e:
            _log.warning("poke_requests sync failed for %s: %s", r.job_id, e)

    page = request_log.page_by_principal(principal, limit=10)
    return RequestLogPage(
        items=_request_items(page.items),
        total_count=page.total_count,
        next_cursor=page.next_cursor,
    )


# ── 퍼블리시 (인라인) ─────────────────────────────────────────────────
@router.post("/api/publish", response_model=PublishResponse)
def publish_asset(req: PublishRequest, request: Request):
    """새 자산을 퍼블리시해요. 등록 → 즉시 승인(auto-approval) → 카탈로그 노출."""
    from .registry.models import RecordAlreadyExists
    from .registry.models import ValidationError as RegValidationError
    registry = get_registry()
    registry_id = get_registry_id()
    principal = _principal(request)
    owner_contact = _owner_contact(request)
    _require_responsibility_contacts(
        owner_contact, req.escalation_contact, trigger="catalog_publish")

    try:
        dtype = DescriptorType(req.descriptor_type)
    except ValueError:
        raise HTTPException(400, f"Invalid descriptor_type: {req.descriptor_type}")

    # 타입별 descriptors 구성
    descriptors = _build_descriptors(dtype, req)

    try:
        rec = registry.create_record(
            registry_id=registry_id,
            name=slugify(req.name),
            descriptor_type=dtype,
            descriptors=descriptors,
            record_version=req.version,
            description=req.description,
            owner_team=_owner_team(request, req.owner_team),
            # 호환을 위해 요청 필드는 받지만 소유자는 검증된 principal로 확정해요.
            owner_user=principal,
            owner_contact=owner_contact,
            escalation_contact=req.escalation_contact,
            tags=tuple(req.tags),
            category=req.category,
        )
        status = rec.status
    except RecordAlreadyExists:
        raise _duplicate_registration_error(slugify(req.name), req.version)
    except RegValidationError as e:
        raise HTTPException(422, str(e))
    _store_owner_email(rec.record_id, request)

    # 등록 hook(ADR-017) — DRAFT는 내부 과도 상태라 응답 전에 최소 PENDING_APPROVAL로
    # 확정해요. 스캔 시작보다 먼저 불러야 전이가 경쟁하지 않아요.
    hook_status = run_registration_hook(
        rec.record_id, principal,
        RegistrationTrigger.CATALOG_PUBLISH)
    status = _status_or(hook_status, status)

    # 등록 후 거버넌스 후처리(자동 스캔 + 중복검토) — 설정 ON일 때만 실행.
    # 비차단(예외 내부 흡수)이라 게시엔 영향 없음.
    from ..governance.post_registration import run_post_registration
    run_post_registration(rec.record_id)
    _note_missing_escalation(rec.record_id, req.escalation_contact, "catalog_publish")

    # 동기 퍼블리시 요청 로그(succeeded) — My Requests에 노출. 배포형과 한 목록으로 합류.
    from .requests.models import RequestKind, status_from_record_status
    _kind = RequestKind.AGENT_JSON if dtype is DescriptorType.AGENT else RequestKind.SKILL
    # 등록 성공이 곧 게시완료가 아니에요 — hook이 확정한 승인 상태(PENDING_APPROVAL 등)를
    # 반영해 My Requests가 스캔·승인 전에 '게시완료'로 오표시되지 않게 해요.
    _log_request(_kind, rec.name, principal,
                 record_id=rec.record_id, status=status_from_record_status(status))
    return PublishResponse(
        record_id=rec.record_id,
        name=rec.name,
        status=status.value,
        message="퍼블리시 완료! 카탈로그에서 검색할 수 있어요.",
    )


# ── 소스 업로드/버전 ──────────────────────────────────────────────────
@router.post("/api/source/publish/init")
def publish_init(req: PublishInitRequest, request: Request):
    source_store = get_source_store()
    principal = _principal(request)
    # 담당자 계약은 **업로드 티켓 발급 시점에** 검증해요 — finalize 까지 간 뒤 거절하면
    # 사용자가 파일을 다 올린 다음에 되돌아와야 해요(CA-31 과 같은 종류의 늦은 거절).
    _require_responsibility_contacts(
        _owner_contact(request), req.escalation_contact, trigger="source_publish")
    if not req.name.strip():
        raise HTTPException(422, "name is required")
    if req.asset_type == "mcp":
        raise HTTPException(
            422,
            "MCP 소스는 배포 입력으로만 업로드할 수 있어요.",
        )
    # owner는 클라이언트 입력 금지 — principal에서 파생(§11.1, §11.6).
    asset_id = f"{slugify(principal)}/{slugify(req.name)}"
    try:
        validate_asset_id(asset_id)
    except ValueError as e:
        raise HTTPException(422, str(e))

    # 버전 자동결정(§11.3): 미지정/비표준 semver면 list_versions로 다음 버전 계산.
    version = (req.version or "").strip()
    if not is_valid_semver(version):
        existing = source_store.list_versions(asset_id)
        if not existing:
            version = "1.0.0"
        else:
            major, minor, patch = parse_semver(existing[-1].version)
            version = f"{major}.{minor}.{patch + 1}"

    meta = {
        "name": req.name, "description": req.description,
        # init 시점에 확정해 finalize까지 운반해요(§11.7) — finalize는 이 meta만 보고
        # 레코드를 만들거든요. 신원의 team이 있으면 그걸 쓰고, 없으면 요청 값을 존중해요.
        "owner_team": _owner_team(request, req.owner_team),
        "escalation_contact": req.escalation_contact,
        "tags": list(req.tags), "category": req.category,
        "changelog": req.changelog,
    }
    try:
        specs = [FileSpec(path=f.path, size=f.size) for f in req.files]
        ticket = source_store.presign_upload(
            asset_id, req.asset_type, version, specs,
            principal=principal, meta=meta,
        )
    except VersionAlreadyExists:
        raise HTTPException(409, f"version already published: {version}")
    except (ValueError, SourceStoreError) as e:
        raise HTTPException(422, str(e))
    return {"asset_id": ticket.asset_id, "version": ticket.version,
            "upload_id": ticket.upload_id, "urls": ticket.urls}


@router.post("/api/source/publish/finalize")
def publish_finalize(req: PublishFinalizeRequest, request: Request):
    """소스를 확정하고, 이어서 카탈로그 레코드를 등재·승인해요 (설계 §6 흐름).

    원자성(§11.5): source 버전 PUBLISH 후 카탈로그 등재가 실패하면 source 버전을
    롤백(보상 트랜잭션)해 orphan을 남기지 않아요.
    """
    from .registry.models import RecordAlreadyExists
    from .registry.models import ValidationError as RegValidationError
    registry = get_registry()
    registry_id = get_registry_id()
    source_store = get_source_store()

    principal = _principal(request)
    try:
        rec = source_store.finalize_version(
            req.asset_id, req.version, req.upload_id, principal=principal,
        )
    except IncompleteUpload as e:
        raise HTTPException(409, f"incomplete upload: {e}")
    except VersionAlreadyExists:
        raise HTTPException(409, f"version already published: {req.version}")
    except VersionNotFound as e:
        raise HTTPException(404, str(e))
    except SourceStoreError as e:
        raise HTTPException(422, str(e))

    if rec.asset_type == "mcp":
        try:
            source_store.rollback_version(
                rec.asset_id,
                rec.version,
                principal=principal,
            )
        except Exception:
            pass
        raise HTTPException(
            422,
            "MCP 소스는 배포 입력으로만 확정할 수 있어요.",
        )

    # 카탈로그 메타는 STAGING 아이템에 durable 저장됐던 값을 그대로 써요(§11.7).
    meta = rec.meta or {}
    # name fallback(§11.6, 숨은 버그 수정): meta.name(비어있지 않으면) →
    # asset_id의 name 세그먼트(슬래시 뒤). 슬래시 포함 asset_id 전체를 name으로 쓰지 않아요.
    name_segment = rec.asset_id.split("/")[-1]
    # Registry name 제약에 맞게 슬러그화(공백·한글 이름도 안전). 표시용 원본은 아직
    # description·소스 파일에 남아요. name_segment는 asset_id에서 온 값이라 이미 slug-safe.
    name = slugify((meta.get("name") or "").strip()) or name_segment
    description = meta.get("description", "")
    owner_team = meta.get("owner_team", "")
    escalation_contact = meta.get("escalation_contact", "")
    tags = meta.get("tags", [])
    category = meta.get("category", "")
    changelog = meta.get("changelog", "")

    try:
        extra = {"s3_prefix": rec.s3_prefix}
        # agent는 업로드된 agent-card.json 본문을 읽어 descriptor에 실어요(A2A 카드 무손실).
        if rec.asset_type == "agent":
            paths = rec.manifest.paths()
            card_path = "agent-card.json" if "agent-card.json" in paths else (
                "agent.json" if "agent.json" in paths else None)
            if card_path:
                try:
                    extra["agent_card"] = source_store.read_file(
                        rec.asset_id, rec.version, card_path).decode("utf-8")
                except Exception:
                    pass  # 카드 읽기 실패 시 sourcePrefix만으로 진행(mapping이 기본 카드 조립)
        descriptors = get_binding(rec.asset_type).build_descriptors(rec.manifest, extra)
        dtype = _ASSET_TYPE_TO_DESCRIPTOR[rec.asset_type]
        catalog_rec = registry.create_record(
            registry_id, name, dtype, descriptors, rec.version,
            description=description, owner_team=owner_team,
            owner_user=principal, tags=tuple(tags), category=category,
            changelog=changelog, escalation_contact=escalation_contact,
            # 1차 담당자는 finalize 를 호출한 검증된 principal 에서 파생해요 (CA-29).
            # init 의 meta 로 운반하지 않는 이유: 같은 사용자가 두 호출을 다 하고,
            # meta 를 거치면 클라이언트가 통제하는 STAGING 값에 의존하게 돼요.
            owner_contact=_owner_contact(request),
        )
        catalog_status = catalog_rec.status  # DRAFT — verdict 통과 시 APPROVED
        catalog_record_id = catalog_rec.record_id
    except Exception as e:
        # 보상 트랜잭션: 카탈로그 등재 실패 → source 버전 롤백(best-effort).
        try:
            source_store.rollback_version(rec.asset_id, rec.version, principal=principal)
        except Exception:
            pass  # 롤백 실패는 무시하고 원인 에러를 surface (포렌식은 S3 Metadata journal).
        if isinstance(e, RecordAlreadyExists):
            # 중복은 서버 결함이 아니라 입력 문제예요 — 500 대신 409 + 행동 안내 (CA-33).
            raise _duplicate_registration_error(name, rec.version)
        if isinstance(e, RegValidationError):
            raise HTTPException(422, f"catalog registration failed (rolled back): {e}")
        raise HTTPException(500, f"catalog registration failed (rolled back): {e}")
    _store_owner_email(catalog_record_id, request)

    # 이전 버전 숨김은 신규가 APPROVED로 전이할 때 처리해요(scan_service.apply_verdict_to_registry).
    # 등록 즉시 숨기면 신규가 DRAFT인 동안 catalog 공백이 생기거든요(설계 §3).

    # 등록 hook(ADR-017) — 스캔 시작 전에 최소 PENDING_APPROVAL로 확정해요.
    hook_status = run_registration_hook(
        catalog_record_id, principal, RegistrationTrigger.SOURCE_PUBLISH)
    catalog_status = _status_or(hook_status, catalog_status)

    # 등록 후 거버넌스 후처리(자동 스캔 + 중복검토) — 설정 ON일 때만 실행.
    # 비차단이라 게시(이미 확정)엔 영향 없음.
    from ..governance.post_registration import run_post_registration
    run_post_registration(catalog_record_id)
    _note_missing_escalation(catalog_record_id, escalation_contact, "source_publish")

    # 요청 로그 — "나의 요청"에 노출돼요. 이 경로에만 빠져 있어서 소스 업로드로 등록한
    # skill/agent가 목록에 아예 안 보였어요(다른 동기 등록 3경로엔 있었음).
    # status는 **게시 축**이라 succeeded예요(심사 진행은 governance 축이 따로 실려요).
    from .requests.models import RequestKind
    # 이 경로는 skill·agent·mcp 세 asset_type을 모두 받아요(`_ASSET_TYPE_TO_DESCRIPTOR` 참조).
    # kind를 하나로 뭉개면 목록 배지가 틀려요. mcp는 배포형(deploy-mcp)이 아니라 소스 업로드라
    # 가장 가까운 기존 kind에 매핑해요 — 새 kind를 추가하면 FE 라벨 맵도 함께 바꿔야 해서
    # 이번 범위 밖이에요.
    _SOURCE_KIND = {
        "skill": RequestKind.SKILL,
        "agent": RequestKind.AGENT_JSON,
        "mcp": RequestKind.DEPLOY_MCP,
    }
    from .requests.models import status_from_record_status
    _log_request(
        _SOURCE_KIND.get(rec.asset_type, RequestKind.SKILL),
        name, principal, record_id=catalog_record_id,
        status=status_from_record_status(catalog_status))

    # status는 source 버전 상태(PUBLISHED), catalog_status는 카탈로그 등재 상태
    # (등록 hook이 최소 PENDING_APPROVAL로 확정, verdict 통과 시 APPROVED로 전이) —
    # 프론트가 상세 화면과 일관되게 표시하도록 분리해 내려줘요.
    return {"asset_id": rec.asset_id, "version": rec.version, "status": rec.status.value,
            "published_by": rec.published_by, "published_at": rec.published_at,
            "files": list(rec.manifest.paths()), "catalog_record_id": catalog_record_id,
            "catalog_status": catalog_status.value}


def _require_public_source_version(asset_id: str, version: str) -> None:
    versions = get_source_store().list_versions(asset_id)
    if any(
        item.version == version and item.asset_type == "mcp"
        for item in versions
    ):
        raise HTTPException(404, "source asset not found")


@router.get("/api/source/assets/{asset_id:path}/versions")
def list_asset_versions(asset_id: str):
    registry = get_registry()
    registry_id = get_registry_id()
    all_records = get_source_store().list_versions(asset_id)
    records = [record for record in all_records if record.asset_type != "mcp"]
    if all_records and not records:
        raise HTTPException(404, "source asset not found")
    # catalog 레코드를 version으로 매핑(changelog·visible·record_id 보강).
    cat = {r.version: r for r in registry.find_by_source_asset_id(registry_id, asset_id)}
    out = []
    for r in records:
        c = cat.get(r.version)
        out.append({
            "version": r.version, "status": r.status.value,
            "published_by": r.published_by, "published_at": r.published_at,
            "file_count": len(r.manifest.paths()),
            "changelog": c.changelog if c else "",
            "search_visible": c.search_visible if c else False,
            "record_id": c.record_id if c else None,
        })
    return out


@router.patch("/api/source/assets/{asset_id:path}/versions/{version}/visibility")
def set_version_visibility(asset_id: str, version: str, req: VisibilityRequest):
    """버전의 메인/검색 노출 여부를 토글해요(§12)."""
    _require_public_source_version(asset_id, version)
    from .registry.models import RecordNotFound
    registry = get_registry()
    registry_id = get_registry_id()
    target = next(
        (r for r in registry.find_by_source_asset_id(registry_id, asset_id)
         if r.version == version),
        None,
    )
    if target is None:
        raise HTTPException(404, f"no catalog record for {asset_id}@{version}")
    try:
        rec = registry.set_search_visible(registry_id, target.record_id, req.visible)
    except RecordNotFound:
        raise HTTPException(404, "record not found")
    return {"asset_id": asset_id, "version": version,
            "search_visible": rec.search_visible, "record_id": rec.record_id}


@router.get("/api/source/assets/{asset_id:path}/versions/{version}/archive")
def download_version_archive(
    asset_id: str, version: str, record_id: str | None = Query(None),
):
    """버전 파일트리를 tar.gz로 내려줘요 — ~/.claude/skills/{name}/ 설치용(§12).

    tar 최상위에 SKILL.md 등 파일이 바로 오게 구성해, `-C ~/.claude/skills/{name}`로
    풀면 그 폴더 안에 트리가 복원돼요.

    record_id가 전달되면 카탈로그 다운로드 카운트를 +1해요(실패는 tar.gz 응답을 막지 않음).
    """
    _require_public_source_version(asset_id, version)
    source_store = get_source_store()
    try:
        manifest = source_store.get_manifest(asset_id, version)
    except VersionNotFound as e:
        raise HTTPException(404, str(e))
    _count_download(record_id)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for path in manifest.paths():
            try:
                data = source_store.read_file(asset_id, version, path)
            except VersionNotFound as e:
                raise HTTPException(404, str(e))
            info = tarfile.TarInfo(name=path)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    buf.seek(0)
    name = asset_id.split("/")[-1]
    return StreamingResponse(
        buf, media_type="application/gzip",
        headers={"Content-Disposition": f'attachment; filename="{name}-{version}.tar.gz"'},
    )


@router.get("/api/source/assets/{asset_id:path}/versions/{version}/files/{path:path}")
def read_source_file(asset_id: str, version: str, path: str):
    """소스 파일 1개를 텍스트로 읽어와요 — 상세 화면의 SKILL.md 본문 표시용(§12).

    소스 모드 자산은 descriptors에 sourcePrefix만 담고 본문은 S3에만 있어서,
    상세 화면이 이 엔드포인트로 실물 본문을 읽어와 렌더해요.
    """
    _require_public_source_version(asset_id, version)
    source_store = get_source_store()
    try:
        data = source_store.read_file(asset_id, version, path)
    except VersionNotFound as e:
        raise HTTPException(404, str(e))
    try:
        content = data.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(415, "binary file — 텍스트로 읽을 수 없어요.")
    return {"asset_id": asset_id, "version": version, "path": path, "content": content}


# ── 설치 (Install) ────────────────────────────────────────────────────
@router.get("/api/install/targets")
def list_install_targets(asset_type: str = Query(...)):
    """자산타입을 지원하는 tool 목록 (드롭다운용)."""
    return [{"tool_id": t.tool_id, "display_name": t.display_name}
            for t in targets_supporting(asset_type)]


@router.get("/api/assets/{record_id}/install")
def get_install_instruction(
    record_id: str,
    tool: str = Query("claude"),
    os: str = Query("macos"),
    version: str | None = Query(None),
):
    """자산을 tool·OS에 설치하는 완성된 명령을 생성해요."""
    from .registry.models import RecordNotFound
    registry = get_registry()
    try:
        rec = registry.get_record(get_registry_id(), record_id)
    except RecordNotFound:
        raise HTTPException(404, "Asset not found")

    # asset_type 도출: descriptor_type → asset_type 역매핑
    dt_to_at = {v: k for k, v in _ASSET_TYPE_TO_DESCRIPTOR.items()}
    asset_type = dt_to_at.get(rec.descriptor_type)
    if asset_type is None:
        raise HTTPException(422, f"{rec.descriptor_type.value}은 설치를 지원하지 않아요.")

    try:
        target = get_target(tool)
    except KeyError:
        raise HTTPException(400, f"unknown tool: {tool}")
    try:
        os_enum = InstallOS(os)
    except ValueError:
        raise HTTPException(400, f"unknown os: {os}")

    recipe = target.recipe_for(asset_type)
    if recipe is None:
        raise HTTPException(422, f"{target.display_name}은 {asset_type} 설치를 지원하지 않아요.")

    if asset_type == "skill":
        # archive_url은 상대 경로로 만들고, 프론트가 {API_BASE}를 실제 origin으로 치환해요
        # (Task 6). 그래서 백엔드는 자기 origin을 몰라도 돼요.
        from urllib.parse import quote
        prefix = rec.source_prefix  # skill/{owner}/{name}/{version}/
        parts = [p for p in prefix.split("/") if p]
        if len(parts) < 4:
            raise HTTPException(422, "설치 가능한 소스 버전이 없어요.")
        asset_id = "/".join(parts[1:-1])
        ver = version or parts[-1]
        # 요청 version이 실제 소스에 존재하는지 확인해요. 없는 버전이면 archive_url만
        # 만들어 200을 돌려줬는데(설치 시 curl이 404로 실패), "그 버전은 없어요"는
        # 설치 미지원(422)이 아니라 자산/버전 없음(404)이라 여기서 404로 구분해 응답해요.
        try:
            get_source_store().get_manifest(asset_id, ver)
        except VersionNotFound:
            raise HTTPException(404, f"{asset_id}@{ver} 버전을 찾을 수 없어요.")
        rel = ("/api/source/assets/"
               + "/".join(quote(s) for s in asset_id.split("/"))
               + f"/versions/{quote(ver)}/archive"
               + f"?record_id={quote(record_id)}")
        ins = recipe.build(rec.name, "{API_BASE}" + rel, os_enum)
        _count_download(record_id)
    else:  # mcp
        endpoint = None
        node = rec.descriptors.get("mcp")
        if isinstance(node, dict):
            endpoint = node.get("endpoint")
        ins = recipe.build(rec.name, endpoint, os_enum)
        _count_download(record_id)

    return {
        "tool_id": tool, "asset_type": asset_type, "os": ins.os,
        "shell": ins.shell, "kind": ins.kind, "command": ins.command,
        "config_snippet": ins.config_snippet, "target_path": ins.target_path,
        "note": ins.note,
    }


# ── MCP 등록 (중앙 호스팅) ────────────────────────────────────────────
@router.post("/api/mcp/register", response_model=McpRegisterResponse)
def register_mcp_asset(reg: McpRegistration, request: Request):
    """연결형 MCP를 등록해요(tool 조회 → Gateway target → hosted).

    배포형 MCP는 이 엔드포인트가 아니라 소스 업로드 배포 흐름(`POST /api/mcp/deploy/*`)을
    써요. 옛 `mode="deploy"`는 410으로 안내해요(아래 참조).
    """
    from .mcp import registry as mcp_reg_mod
    from .registry.models import RecordAlreadyExists
    from .registry.models import ValidationError as RegValidationError
    from .requests.models import RequestKind
    registry = get_registry()
    registry_id = get_registry_id()
    principal = _principal(request)
    owner_contact = _owner_contact(request)

    if reg.mode not in ("connect", "deploy"):
        raise HTTPException(400, f"unknown mode: {reg.mode}")
    if reg.mode == "deploy":
        # 410을 **Gateway 배선보다 먼저** 확정해요. `register_mcp` 인자로 평가하면
        # `get_connect_gateway_service()`가 먼저 만들어져서, Gateway 설정이 없는 환경에서는
        # 결정적인 410 대신 500이 나요(제거된 모드인데 원인이 엉뚱하게 보여요).
        raise HTTPException(410, {
            "message": (
                "배포형 MCP는 이 엔드포인트로 등록할 수 없어요. "
                "소스 업로드 배포 흐름(POST /api/mcp/deploy/init)을 사용해 주세요."
            ),
            "remediation": "POST /api/mcp/deploy/init 으로 소스를 업로드해 배포해 주세요.",
        })
    _require_responsibility_contacts(
        owner_contact, reg.escalation_contact, trigger="mcp_connect")
    from ...shared.slug import (
        find_gateway_target_name_collision,
        gateway_target_name,
        gateway_target_name_collision_detail,
    )
    from .registry.models import RecordStatus

    target_name = gateway_target_name(reg.name)
    try:
        records = registry.list_records(
            registry_id,
            statuses=tuple(RecordStatus),
            max_results=None,
        )
    except Exception as exc:
        _log.exception("MCP Target 이름 충돌 검사를 수행하지 못했어요.")
        raise HTTPException(503, {
            "message": "Gateway Target 이름 충돌을 확인할 수 없어요.",
            "remediation": "잠시 뒤 다시 등록해 주세요.",
            "target_name": target_name,
        }) from exc
    collision = find_gateway_target_name_collision(
        records,
        target_name=target_name,
    )
    if collision is not None:
        raise HTTPException(409, gateway_target_name_collision_detail(collision))
    try:
        # fetch는 모듈 속성을 호출시점에 참조해요 — 테스트가 fetch_mcp_tools를
        # monkeypatch 하면 그 값이 반영돼야 하거든요(def-time 바인딩 회피).
        from ...shared.deps import get_connect_gateway_service
        gateway_svc = get_connect_gateway_service()
        result = register_mcp(reg, fetch=mcp_reg_mod.fetch_mcp_tools)
        prepared = gateway_svc.prepare_target(reg.name)
        if prepared.get("target_name") != target_name:
            raise RuntimeError("Gateway target 이름 계산이 공용 규칙과 어긋났어요.")
        mcp_node = result.descriptors["mcp"]
        mcp_node.update({
            "endpoint": prepared["gateway_url"],
            "upstreamEndpoint": reg.endpoint,
            "gatewayIdentifier": prepared["gateway_id"],
            "gatewayTargetName": target_name,
            # target보다 Registry가 먼저 생겨야 무기록 인가 표면이 없어요. 생성이나
            # descriptor 확정 중 장애가 나도 이 좌표로 cleanup 대상을 찾을 수 있어요.
            "gatewayTargetState": "provisioning",
        })
    except McpDeployModeRemoved as e:
        # 410 Gone — 있었던 경로를 없앤 것이라는 뜻이에요. 400 `unknown mode`로 뭉개면 옛
        # 클라이언트가 오타로 오해하고, 200 pending을 계속 돌려주면 진전되지 않는 자산이
        # 카탈로그에 쌓여요.
        raise HTTPException(410, {
            "message": str(e),
            "remediation": "POST /api/mcp/deploy/init 으로 소스를 업로드해 배포해 주세요.",
        })
    except McpEndpointError as e:
        # endpoint 입력 검증 실패(주입·SSRF·스킴 차단) — 조회 실패와 같은 422 계열.
        raise HTTPException(422, str(e))
    except (McpProtocolError, McpHealthError) as e:
        # tool 조회 실패(도달 불가·initialize/tools/list 실패·tool 0개 등) → 422.
        raise HTTPException(422, str(e))
    except RuntimeError as e:
        raise HTTPException(503, str(e))

    try:
        rec = registry.create_record(
            registry_id, slugify(reg.name), DescriptorType.MCP, result.descriptors,
            "1.0.0", description=reg.description,
            owner_team=_owner_team(request, reg.owner_team),
            escalation_contact=reg.escalation_contact,
            owner_contact=owner_contact,
            owner_user=principal, tags=tuple(reg.tags), category=reg.category,
        )
        status = rec.status
    except RecordAlreadyExists:
        error = _duplicate_registration_error(slugify(reg.name), "1.0.0")
        _log_failed_request(
            RequestKind.MCP_CONNECT, slugify(reg.name), principal,
            reason=error.detail["message"], remediation=error.detail["remediation"])
        raise error
    except RegValidationError as e:
        _log_failed_request(
            RequestKind.MCP_CONNECT, slugify(reg.name), principal, reason=str(e))
        raise HTTPException(422, str(e))

    target = None
    try:
        tools_inline = result.descriptors["mcp"]["tools"]["inlineContent"]
        target = gateway_svc.register_target(
            reg.name,
            reg.endpoint or "",
            tools_inline,
        )
        if target.get("target_name") not in (None, target_name):
            raise RuntimeError("생성된 Gateway target 이름이 공용 규칙과 어긋났어요.")
        completed_descriptors = {
            **result.descriptors,
            "mcp": {
                **result.descriptors["mcp"],
                "endpoint": target["gateway_url"],
                "gatewayIdentifier": target["gateway_id"],
                "gatewayTargetId": target["target_id"],
                "gatewayTargetName": target_name,
                "gatewayTargetState": "ready",
            },
        }
        rec = registry.update_record_descriptors(
            registry_id,
            rec.record_id,
            rec.name,
            DescriptorType.MCP,
            completed_descriptors,
            rec.version,
            description=reg.description,
        )
        status = rec.status
    except Exception as e:
        # target 생성 뒤 descriptor 확정만 실패한 경우에는 이번 호출이 만든
        # target만 exact gateway/id로 보상 삭제해요. 보상도 실패하면 기록 우선
        # descriptor의 gateway/name/provisioning 상태가 durable cleanup 좌표예요.
        leaked_target = False
        if target and target.get("target_id"):
            try:
                gateway_svc.teardown(
                    target["target_id"],
                    gateway_id=target.get("gateway_id", ""),
                )
            except Exception:
                leaked_target = True
                _log.exception(
                    "connect target compensation failed; durable provisioning "
                    "descriptor remains for record %s",
                    rec.record_id,
                )
        # CA-32: 레코드를 회수해요. 예전에는 target 없는 레코드가 그대로 남아서, 호출이
        # 불가능한 자산이 카탈로그·드리프트 화면에 정상 자산처럼 보였어요(선언↔실체 어긋남).
        # 회수는 IH-81 이 만든 삭제 경로(PurgeService — Cedar permit·Cognito client·인가
        # 원장·요청로그까지)를 재사용해요. 회수 실패는 숨기지 않고 응답에 실어요.
        #
        # **예외 하나**: 보상 삭제가 실패해 Gateway 에 target 이 남았으면 레코드를 지우지
        # 않아요. 그 좌표(gatewayIdentifier·gatewayTargetName·provisioning)가 남은 target 을
        # 찾을 유일한 durable 단서라서, 레코드를 지우면 관측 불가한 유령 target 이 돼요.
        # 이 레코드는 인벤토리에서 `gateway_target_state=provisioning` 으로 드러나요.
        reclaimed = (
            False
            if leaked_target
            else _reclaim_failed_registration(rec.record_id, actor=principal)
        )
        reason = f"{type(e).__name__}: {e}"
        detail = {
            "message": (
                "MCP 를 Gateway 에 연결하지 못해 등록을 되돌렸어요 "
                "(M2 OAuth Gateway target provisioning failed)."
            ),
            "remediation": (
                "endpoint 가 공개 `https://` 주소인지 확인하고 다시 등록해 주세요. "
                "같은 오류가 반복되면 이 사유를 관리자에게 알려주세요."
            ),
            "reason": reason,
            "record_reclaimed": reclaimed,
        }
        if not reclaimed:
            # 회수하지 못했다면 그 사실과 좌표를 남겨요 — "되돌렸다"고 말하면 안 돼요.
            detail["message"] = (
                "MCP 를 Gateway 에 연결하지 못했어요. 등록 레코드를 회수하지 못해 "
                "관리자 정리가 필요해요 (M2 OAuth Gateway target provisioning failed)."
            )
            detail["record_id"] = rec.record_id
            detail["gateway_target_leaked"] = leaked_target
        _log_failed_request(
            RequestKind.MCP_CONNECT, rec.name, principal,
            reason=reason, remediation=detail["remediation"],
            record_id=None if reclaimed else rec.record_id)
        raise HTTPException(502, detail)

    _store_owner_email(rec.record_id, request)
    _seed_mcp_tool_drift(rec.record_id)

    # 등록 hook(ADR-017 결정 1) — 레코드 생성 뒤로 옮겼어요. 이전에는 create_record 전에
    # 판정해서 record_id·정규화된 타입·버전 없이 이름만 보고 결정했고, 반려도 감사에
    # 남지 않았어요. 반려는 여기서 durable REJECTED 레코드를 남기고 403이에요.
    hook_status = run_registration_hook(
        rec.record_id, principal, RegistrationTrigger.MCP_CONNECT)
    status = _status_or(hook_status, status)

    # 등록 후 거버넌스 후처리(자동 스캔 + 중복검토) — 설정 ON일 때만 실행. 비차단.
    from ..governance.post_registration import run_post_registration
    run_post_registration(rec.record_id)
    _note_missing_escalation(rec.record_id, reg.escalation_contact, "mcp_connect")

    # 동기 등록 요청 로그(succeeded) — My Requests에 노출.
    from .requests.models import RequestKind, status_from_record_status
    _log_request(RequestKind.MCP_CONNECT, rec.name, principal,
                 record_id=rec.record_id, status=status_from_record_status(status))

    msg = ("연결형 MCP를 등록했어요. 카탈로그에서 설치할 수 있어요."
           if result.hosting == "hosted"
           else "배포형 MCP를 등록했어요. 배포 완료 후 설치할 수 있어요.")
    return {"record_id": rec.record_id, "name": rec.name,
            "hosting": result.hosting, "status": status.value, "message": msg}


@router.post("/api/mcp/connect-test")
def mcp_connect_test(req: McpConnectTestRequest):
    """MCP endpoint에 실제 핸드셰이크로 tool 목록을 미리 가져와요(등록 안 함)."""
    from .mcp import protocol as _p
    from .mcp.registry import validate_endpoint
    try:
        validate_endpoint(req.endpoint or "")
        info = _p.fetch_mcp_tools(req.endpoint)
    except McpEndpointError as e:
        raise HTTPException(422, str(e))
    except McpProtocolError as e:
        raise HTTPException(422, str(e))
    if not info.tools:
        raise HTTPException(422, "tool을 하나도 제공하지 않는 MCP는 등록할 수 없어요.")
    return {"ok": True, "name": info.name, "instructions": info.instructions,
            "tools": [{"name": t.name, "description": t.description} for t in info.tools]}


# ── A2A Agent (도메인 연결) ──────────────────────────────────────────
@router.post("/api/agent/connect-test")
def agent_connect_test(req: AgentConnectTestRequest):
    """도메인의 .well-known/agent-card.json을 GET해 정체성·skills·보안요구를 미리 보여줘요(등록 안 함)."""
    from .a2a import A2AProtocolError, fetch_agent_card
    try:
        card = fetch_agent_card(req.endpoint or "")
    except McpEndpointError as e:      # validate_endpoint 재사용(SSRF·주입) — 입력 검증 실패
        raise HTTPException(422, str(e))
    except A2AProtocolError as e:      # 도달 불가·비-JSON·필수 필드 누락
        raise HTTPException(422, str(e))
    return {
        "ok": True,
        "name": card.name,
        "description": card.description,
        "version": card.version,
        "protocol_version": card.protocol_version,
        "url": card.url,
        "capabilities": card.capabilities,
        "security_schemes": list(card.security_schemes),
        "skills": [
            {"id": s.id, "name": s.name, "description": s.description, "tags": list(s.tags)}
            for s in card.skills
        ],
    }


@router.post("/api/agent/register", response_model=AgentRegisterResponse)
def register_agent_asset(reg: AgentRegistration, request: Request):
    """도메인 연결형 A2A agent를 카탈로그에 등록해요. connect 후 agent-card를 확정 저장."""
    from .a2a import A2AProtocolError, fetch_agent_card
    from .registry.models import RecordAlreadyExists
    from .registry.models import ValidationError as RegValidationError
    from .requests.models import RequestKind
    registry = get_registry()
    registry_id = get_registry_id()
    principal = _principal(request)
    owner_contact = _owner_contact(request)
    _require_responsibility_contacts(
        owner_contact, reg.escalation_contact, trigger="agent_connect")

    # 등록 시점에 카드를 다시 가져와 최신본을 확정(연결 테스트 이후 변경 대비).
    try:
        card = fetch_agent_card(reg.endpoint or "")
    except McpEndpointError as e:
        raise HTTPException(422, str(e))
    except A2AProtocolError as e:
        raise HTTPException(422, str(e))

    raw_name = (reg.name or card.name or "").strip()
    if not raw_name:
        raise HTTPException(422, "agent 이름을 확인할 수 없어요 (카드 name 없음).")
    # Registry name 제약([a-zA-Z0-9][a-zA-Z0-9_\-\.\/]*)에 맞게 슬러그화 —
    # "Agent Tools Directory"(공백) 같은 카드 이름도 안전하게 등록돼요. 원래 이름은
    # description 앞부분/agentCard 원본(inlineContent)에 그대로 보존돼요.
    name = slugify(raw_name)
    description = reg.description or (card.description or "")

    # descriptors: endpoint + 원본 카드(inlineContent)를 담아 aws_mapping이 a2a.agentCard로 변환.
    descriptors = {
        "agent": {
            "endpoint": card.url or reg.endpoint,
            "agentCard": {"inlineContent": json.dumps(card.raw, ensure_ascii=False)},
        }
    }
    try:
        rec = registry.create_record(
            registry_id, name, DescriptorType.AGENT, descriptors,
            card.version or "1.0.0", description=description,
            owner_team=_owner_team(request, reg.owner_team),
            escalation_contact=reg.escalation_contact,
            owner_contact=owner_contact,
            owner_user=principal, tags=tuple(reg.tags), category=reg.category,
        )
        status = rec.status
    except RecordAlreadyExists:
        error = _duplicate_registration_error(name, card.version or "1.0.0")
        _log_failed_request(
            RequestKind.AGENT_DOMAIN, name, principal,
            reason=error.detail["message"], remediation=error.detail["remediation"])
        raise error
    except RegValidationError as e:
        _log_failed_request(RequestKind.AGENT_DOMAIN, name, principal, reason=str(e))
        raise HTTPException(422, str(e))
    _store_owner_email(rec.record_id, request)

    # 등록 hook(ADR-017 결정 1) — MCP 연결형과 같은 지점·같은 계약이에요.
    hook_status = run_registration_hook(
        rec.record_id, principal, RegistrationTrigger.AGENT_CONNECT)
    status = _status_or(hook_status, status)

    # 등록 후 거버넌스 후처리(자동 스캔 + 중복검토) — 설정 ON일 때만 실행. 비차단.
    from ..governance.post_registration import run_post_registration
    run_post_registration(rec.record_id)
    _note_missing_escalation(rec.record_id, reg.escalation_contact, "agent_connect")

    # 동기 등록 요청 로그(succeeded) — My Requests에 노출.
    from .requests.models import RequestKind, RequestStatus
    _log_request(RequestKind.AGENT_DOMAIN, rec.name, principal,
                 record_id=rec.record_id, status=RequestStatus.SUCCEEDED)

    return AgentRegisterResponse(
        record_id=rec.record_id, name=rec.name, status=status.value,
        message="연결형 Agent를 등록했어요. 카탈로그에서 확인할 수 있어요.",
    )
