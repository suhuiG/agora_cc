"""런타임 도메인 라우터 — MCP·agent 배포 → 게이트웨이/AgentCore Runtime 배선.

오너: 런타임 도메인. 배포 job 생성·폴링과 배포 완료 후 카탈로그 등재를 담당해요.
SoT(AgoraCatalog)는 카탈로그 도메인가 소유하므로, 여기서는 shared.deps 접근자와
카탈로그 라우터가 노출하는 공용 헬퍼(_principal·_require_owner·_log_request)로만 접근해요.

이 라우트들은 원래 카탈로그 라우터에 있었어요(W0 선행 refactor로 분리). 경로·응답 모델은
계약 안정을 위해 그대로 유지해요(`/api/mcp/deploy/*`, `/api/agent/deploy/*`).
"""

from __future__ import annotations

import hashlib
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from ..catalog.mcp.models import (
    AgentDeployFinalizeRequest,
    AgentDeployInitRequest,
    AgentDeployResponse,
    AgentDeployStartRequest,
    McpDeployFinalizeRequest,
    McpDeployInitRequest,
    McpDeployResponse,
    McpDeployStartRequest,
)
from ..catalog.router import (
    _log_request,
    _owner_contact,
    _owner_team,
    _principal,
    _require_owner,
    _require_responsibility_contacts,
)
from ..catalog.sourcestore import (
    FileSpec,
    IncompleteUpload,
    SourceStoreError,
    VersionAlreadyExists,
    VersionNotFound,
    validate_asset_id,
)
from ..catalog.sourcestore.semver import parse_semver
from ..identity.context import current_principal
from .deploy.models import DeployPhase
from .deploy.service import inherit_agent_redeploy_metadata
from .schemas import (
    RuntimeDeploymentDetail,
    RuntimeDeploymentPage,
    deployment_detail,
    deployment_summary,
)

router = APIRouter(tags=["runtime"])


def _require_runtime_admin(request: Request) -> None:
    # 배포 오류와 검증 결과는 운영 상태이므로 일반 사용자에게 노출하지 않고 admin으로 제한해요.
    if not current_principal(request).is_admin:
        raise HTTPException(403, "권한 없음 (런타임 관리 조회는 admin 전용)")


@router.get(
    "/api/runtime/deployments",
    response_model=RuntimeDeploymentPage,
    dependencies=[Depends(_require_runtime_admin)],
)
def list_runtime_deployments(
    asset_type: Literal["mcp", "agent"] | None = Query(None),
    phase: DeployPhase | None = Query(None),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
) -> RuntimeDeploymentPage:
    """배포 job 기반 런타임 운영 상태를 최신순으로 조회해요."""
    from ...shared.deps import get_deploy_service

    jobs = get_deploy_service().list_deployments()
    if asset_type is not None:
        jobs = [job for job in jobs if job.asset_type == asset_type]
    if phase is not None:
        jobs = [job for job in jobs if job.phase is phase]
    jobs.sort(
        key=lambda job: (job.updated_at, job.created_at, job.job_id),
        reverse=True,
    )
    total = len(jobs)
    page = jobs[offset:offset + limit]
    return RuntimeDeploymentPage(
        items=[deployment_summary(job) for job in page],
        total=total,
        offset=offset,
        limit=limit,
    )


@router.get(
    "/api/runtime/deployments/{job_id}",
    response_model=RuntimeDeploymentDetail,
    dependencies=[Depends(_require_runtime_admin)],
)
def get_runtime_deployment(job_id: str) -> RuntimeDeploymentDetail:
    """배포 job 한 건의 portal-safe 상세 상태를 조회해요."""
    from ...shared.deps import get_deploy_service

    job = get_deploy_service().get_deployment(job_id)
    if job is None:
        raise HTTPException(404, "deploy job not found")
    return deployment_detail(job)


# ── MCP 배포 (deploy 모드 — 소스 업로드 → 빌드·런타임·게이트웨이 배선) ──
def _deploy_resp(job) -> McpDeployResponse:
    """DeployJob → McpDeployResponse 응답 변환 헬퍼(deploy·poll 공용)."""
    return McpDeployResponse(
        job_id=job.job_id, phase=job.phase.value, build_type=job.build_type,
        endpoint=job.gateway_url,  # Task 1에서 endpoint→gateway_url로 변경; 응답 키는 endpoint로 유지(API 계약 안정)
        record_id=job.record_id, error=job.error,
        updated_at=job.updated_at,
        gateway_targets=list(job.gateway_targets),
        gateway_target_quota=job.gateway_target_quota,
        unassigned_tools=list(job.unassigned_tools),
    )


@router.post("/api/mcp/deploy/init")
def mcp_deploy_init(req: McpDeployInitRequest, request: Request):
    """배포형 MCP 소스 업로드 티켓을 발급해요 (asset_type=mcp 고정)."""
    from ...shared.deps import get_source_store
    from ...shared.slug import slugify
    source_store = get_source_store()
    principal = _principal(request)
    # 담당자 계약을 **업로드 전에** 검증해요 (CA-29 · ADR-0069). 소스를 다 올린 뒤
    # 거절하면 사용자가 처음부터 다시 해야 해요.
    owner_contact = _owner_contact(request)
    _require_responsibility_contacts(
        owner_contact, req.escalation_contact, trigger="deploy_init")
    if not req.name.strip():
        raise HTTPException(422, "name is required")
    # owner는 클라이언트 입력 금지 — principal에서 파생(§11.1, §11.6).
    asset_id = f"{slugify(principal)}/{slugify(req.name)}"
    try:
        validate_asset_id(asset_id)
    except ValueError as e:
        raise HTTPException(422, str(e))
    # 버전 자동결정: 기존 버전이 없으면 1.0.0, 있으면 patch +1.
    existing = source_store.list_versions(asset_id)
    if not existing:
        version = "1.0.0"
    else:
        major, minor, patch = parse_semver(existing[-1].version)
        version = f"{major}.{minor}.{patch + 1}"
    meta = {"name": req.name, "description": req.description,
            # 검증된 IdP team이 있으면 그걸 써요 — 동기 등록·source publish와 같은 규칙이에요.
            # 요청 값을 그대로 믿으면 남의 팀 이름으로 자산을 만들어 소유 추적이 위조돼요.
            "owner_team": _owner_team(request, req.owner_team),
            "tags": list(req.tags), "category": req.category,
            # 1차 담당자는 서버가 principal 에서 파생해요 (CA-29). 배포 job 은 나중에
            # 다른 프로세스에서 레코드를 만들어서, 이 값을 티켓 meta 로 운반해야 해요.
            "owner_contact": owner_contact,
            "escalation_contact": req.escalation_contact}
    try:
        specs = [FileSpec(path=f.path, size=f.size) for f in req.files]
        ticket = source_store.presign_upload(
            asset_id, "mcp", version, specs, principal=principal, meta=meta)
    except VersionAlreadyExists:
        raise HTTPException(409, f"version already published: {version}")
    except (ValueError, SourceStoreError) as e:
        raise HTTPException(422, str(e))
    return {"asset_id": ticket.asset_id, "version": ticket.version,
            "upload_id": ticket.upload_id, "urls": ticket.urls}


@router.post("/api/mcp/deploy/finalize")
def mcp_deploy_finalize(req: McpDeployFinalizeRequest, request: Request):
    """소스 버전을 확정(PUBLISHED)해요. 카탈로그 등재는 배포 job이 완주 후에 해요."""
    from ...shared.deps import get_source_store
    source_store = get_source_store()
    principal = _principal(request)
    try:
        rec = source_store.finalize_version(
            req.asset_id, req.version, req.upload_id, principal=principal)
    except IncompleteUpload as e:
        raise HTTPException(409, f"incomplete upload: {e}")
    except VersionAlreadyExists:
        raise HTTPException(409, f"version already published: {req.version}")
    except VersionNotFound as e:
        raise HTTPException(404, str(e))
    except SourceStoreError as e:
        raise HTTPException(422, str(e))
    return {"asset_id": rec.asset_id, "version": rec.version,
            "files": list(rec.manifest.paths())}


class McpToolsPreviewRequest(BaseModel):
    asset_id: str
    version: str


@router.post("/api/mcp/deploy/tools-preview")
def mcp_deploy_tools_preview(req: McpToolsPreviewRequest, request: Request):
    """업로드된 배포형 MCP 소스를 정적 파싱해 tool 목록을 미리 보여줘요(등록 안 함).

    connect-test의 배포판이에요. 코드를 실행하지 않고 ast로만 @mcp.tool을 찾아요.
    """
    from ...shared.deps import get_source_store
    from .deploy.tool_extract import discover_mcp_tools

    # principal 인증만 해요 — 소유권은 강제하지 않고 asset_id로 소스를 읽어요(mcp_deploy_start와 동일 패턴). 노출은 tool 이름·docstring 첫 줄로 한정.
    _principal(request)
    source_store = get_source_store()
    versions = source_store.list_versions(req.asset_id)
    match = next((v for v in versions if v.version == req.version), None)
    if match is None:
        raise HTTPException(404, "업로드된 소스 버전을 찾을 수 없어요.")
    files: dict[str, str] = {}
    for path in match.manifest.paths():
        if not path.endswith(".py"):
            continue
        try:
            raw = source_store.read_file(req.asset_id, req.version, path)
        except Exception:
            continue
        # 비UTF-8(CP949/EUC-KR 등) 소스도 500 없이 관대하게 디코드해요
        # (tool_extract.py:160의 errors="ignore" 관례와 일치).
        files[path] = raw.decode("utf-8", errors="ignore") if isinstance(raw, bytes) else str(raw)
    return {"tools": discover_mcp_tools(files)}


@router.post("/api/mcp/deploy", response_model=McpDeployResponse)
def mcp_deploy_start(req: McpDeployStartRequest, request: Request):
    """확정된 소스 버전으로 배포 job을 시작해요 (승인 게이트 → QUEUED)."""
    from ...shared.deps import get_deploy_service, get_registry, get_registry_id, get_source_store
    from ...shared.slug import (
        find_gateway_target_name_collision,
        gateway_target_name,
        gateway_target_name_collision_detail,
        resolved_gateway_target_name,
    )
    from ..catalog.registry.models import RecordStatus
    from .deploy.models import GateRejected, SourceRef, SpecCheckError
    principal = _principal(request)
    svc = get_deploy_service()
    # 메타는 소스 STAGING→PUBLISHED에 durable 저장됐던 값을 쓰되, MVP는 versions에서 조회.
    versions = get_source_store().list_versions(req.asset_id)
    match = next((v for v in versions if v.version == req.version), None)
    meta = (match.meta if match else {}) or {"name": req.asset_id.split("/")[-1]}
    # 재배포면 소유권을 확인하고 이름을 기존 레코드에 고정해요 — 이름이 다르면 다른 Lambda/target을
    # 갱신해버려요. 같은 함수명·target명 → 같은 ARN·endpoint 유지(ADR-0021).
    if req.redeploy_record_id:
        from ..catalog.registry.models import DescriptorType
        rec = _require_owner(req.redeploy_record_id, principal)   # 404/403 게이트
        if rec.descriptor_type is not DescriptorType.MCP:
            raise HTTPException(422, "MCP 자산만 이 경로로 재배포할 수 있어요.")
        meta = {**meta, "name": rec.name}
        mcp = (rec.descriptors or {}).get("mcp") or {}
        target_name = resolved_gateway_target_name(
            rec.name,
            mcp.get("gatewayTargetName") if isinstance(mcp, dict) else "",
        )
    else:
        target_name = gateway_target_name(
            str(meta.get("name") or req.asset_id.split("/")[-1])
        )
    try:
        records = get_registry().list_records(
            get_registry_id(),
            statuses=tuple(RecordStatus),
            max_results=None,
        )
    except Exception as exc:
        raise HTTPException(503, {
            "message": "Gateway Target 이름 충돌을 확인할 수 없어요.",
            "remediation": "잠시 뒤 다시 배포해 주세요.",
            "target_name": target_name,
        }) from exc
    collision = find_gateway_target_name_collision(
        records,
        target_name=target_name,
        current_record_id=req.redeploy_record_id,
    )
    if collision is not None:
        raise HTTPException(409, gateway_target_name_collision_detail(collision))
    meta = {**meta, "_gateway_target_name": target_name}
    try:
        job = svc.create_job(
            SourceRef(req.asset_id, req.version),
            meta,
            principal,
            selected_tools=req.selected_tools,
            redeploy_record_id=req.redeploy_record_id,
        )
    except SpecCheckError as e:
        raise HTTPException(422, str(e))
    except GateRejected as e:
        raise HTTPException(403, str(e))
    # detached 완주용 백그라운드 kick은 프론트 폴링이 대신하므로 MVP는 생략(폴링이 진행).
    from ..catalog.requests.models import RequestKind
    title = meta.get("name") or req.asset_id.split("/")[-1]
    _log_request(RequestKind.DEPLOY_MCP,
                 f"{title} 재배포" if req.redeploy_record_id else title,
                 principal, job_id=job.job_id)
    return _deploy_resp(job)


@router.get("/api/mcp/deploy/{job_id}", response_model=McpDeployResponse)
def mcp_deploy_poll(job_id: str):
    """배포 job 상태를 조회하며 한 단계 전진해요(poll-driven advance)."""
    from ...shared.deps import get_deploy_service
    job = get_deploy_service().poll(job_id)
    if job is None:
        raise HTTPException(404, "deploy job not found")
    # 배포 완료로 카탈로그 등재됐으면 등록 hook + auto_scan(등록 경로와 일관). record_id는
    # READY에서만 설정되고, 두 훅 모두 멱등이라 poll 재호출에도 중복 실행되지 않아요.
    # agent poll(agent_deploy_poll)과 서버 poller가 쓰는 것과 같은 함수를 써야 해요 —
    # 예전엔 여기서 maybe_auto_scan만 불러서, auto_scan=off인 기본 설정에서 MCP 배포
    # 레코드가 DRAFT에 고립됐어요(등록 hook을 안 타는 유일한 등재 경로였어요).
    if job.record_id and job.phase is DeployPhase.READY:
        from ..governance.auto_scan import process_deploy_governance
        process_deploy_governance(job)
    return _deploy_resp(job)


# ── Agent 배포 (deploy 모드 — 소스 업로드 → 빌드·AgentCore Runtime 배선) ──
_BEDROCK_AGENTCORE_ENDPOINT = (
    "https://bedrock-agentcore.{region}.amazonaws.com/runtimes/{runtime_arn}/invocations"
)


def _agent_deploy_resp(job, region: str) -> AgentDeployResponse:
    """DeployJob → AgentDeployResponse 변환 헬퍼(agent deploy·poll 공용).

    region은 매 poll마다 load_config()를 재호출하지 않도록 서비스에서 1회 읽은 값을 받아요
    (MCP 경로가 region을 서비스 생성 시 1회만 읽는 것과 동일한 관례).
    """
    endpoint = None
    if job.runtime_arn and job.phase is DeployPhase.READY:
        endpoint = _BEDROCK_AGENTCORE_ENDPOINT.format(
            region=region, runtime_arn=job.runtime_arn)
    return AgentDeployResponse(
        job_id=job.job_id, phase=job.phase.value,
        build_type=job.build_type,
        runtime_arn=job.runtime_arn,
        endpoint=endpoint,
        record_id=job.record_id, error=job.error,
        updated_at=job.updated_at,
        verify_report=job.verify_report,
    )


@router.post("/api/agent/deploy/init")
def agent_deploy_init(req: AgentDeployInitRequest, request: Request):
    """배포형 Agent 소스 업로드 티켓을 발급해요 (asset_type=agent 고정)."""
    from ...shared.deps import get_source_store
    from ...shared.slug import slugify
    source_store = get_source_store()
    principal = _principal(request)
    # 담당자 계약을 **업로드 전에** 검증해요 (CA-29 · ADR-0069). 소스를 다 올린 뒤
    # 거절하면 사용자가 처음부터 다시 해야 해요.
    owner_contact = _owner_contact(request)
    _require_responsibility_contacts(
        owner_contact, req.escalation_contact, trigger="deploy_init")
    if not req.name.strip():
        raise HTTPException(422, "name is required")
    asset_id = f"{slugify(principal)}/{slugify(req.name)}"
    try:
        validate_asset_id(asset_id)
    except ValueError as e:
        raise HTTPException(422, str(e))
    existing = source_store.list_versions(asset_id)
    if not existing:
        version = "1.0.0"
    else:
        major, minor, patch = parse_semver(existing[-1].version)
        version = f"{major}.{minor}.{patch + 1}"
    meta = {"name": req.name, "description": req.description,
            # 검증된 IdP team이 있으면 그걸 써요 — 동기 등록·source publish와 같은 규칙이에요.
            # 요청 값을 그대로 믿으면 남의 팀 이름으로 자산을 만들어 소유 추적이 위조돼요.
            "owner_team": _owner_team(request, req.owner_team),
            "tags": list(req.tags), "category": req.category,
            # 1차 담당자는 서버가 principal 에서 파생해요 (CA-29).
            "owner_contact": owner_contact,
            "escalation_contact": req.escalation_contact,
            "asset_type": "agent", "deployment_source": req.deployment_source,
            "tool_requests": [
                item.model_dump() for item in req.tool_requests
            ]}
    if req.model is not None:
        meta["model"] = req.model
    try:
        specs = [FileSpec(path=f.path, size=f.size) for f in req.files]
        ticket = source_store.presign_upload(
            asset_id, "agent", version, specs, principal=principal, meta=meta)
    except VersionAlreadyExists:
        raise HTTPException(409, f"version already published: {version}")
    except (ValueError, SourceStoreError) as e:
        raise HTTPException(422, str(e))
    return {"asset_id": ticket.asset_id, "version": ticket.version,
            "upload_id": ticket.upload_id, "urls": ticket.urls}


@router.post("/api/agent/deploy/finalize")
def agent_deploy_finalize(req: AgentDeployFinalizeRequest, request: Request):
    """소스 버전을 확정(PUBLISHED)해요. 카탈로그 등재는 배포 job이 완주 후에 해요."""
    from ...shared.deps import get_source_store
    source_store = get_source_store()
    principal = _principal(request)
    try:
        rec = source_store.finalize_version(
            req.asset_id, req.version, req.upload_id, principal=principal)
    except IncompleteUpload as e:
        raise HTTPException(409, f"incomplete upload: {e}")
    except VersionAlreadyExists:
        raise HTTPException(409, f"version already published: {req.version}")
    except VersionNotFound as e:
        raise HTTPException(404, str(e))
    except SourceStoreError as e:
        raise HTTPException(422, str(e))
    return {"asset_id": rec.asset_id, "version": rec.version,
            "files": list(rec.manifest.paths())}


@router.post("/api/agent/deploy", response_model=AgentDeployResponse)
def agent_deploy_start(req: AgentDeployStartRequest, request: Request):
    """확정된 agent 소스 버전으로 배포 job을 시작해요 (승인 게이트 → QUEUED)."""
    from ...shared.deps import get_deploy_service, get_source_store
    from .deploy.models import (
        ConcurrentJobAdmission, GateRejected, RuntimeNameConflict, SourceRef,
        SpecCheckError,
    )
    principal = _principal(request)
    svc = get_deploy_service()
    job_id = ""
    if req.request_id:
        identity = "\0".join((
            principal, req.asset_id, req.version, req.request_id,
        ))
        job_id = f"agent-client-{hashlib.sha256(identity.encode()).hexdigest()[:32]}"
    versions = get_source_store().list_versions(req.asset_id)
    match = next((v for v in versions if v.version == req.version), None)
    meta = (match.meta if match else {}) or {"name": req.asset_id.split("/")[-1]}
    # asset_type=agent를 명시 — DeployService.create_job이 meta["asset_type"]으로 분기
    meta = {**meta, "asset_type": "agent"}
    # 이 키는 Registry에서만 채우는 내부 값이에요. SourceStore meta가 같은 이름을
    # 넣어도 신뢰하지 않도록 먼저 제거합니다.
    meta.pop("_existing_model", None)
    # 재배포면 소유권을 확인하고 갱신 대상 runtime을 찾아요. 이름은 기존 레코드를
    # 따라가야 해요 — meta의 이름이 다르면 다른 runtime을 갱신해버릴 수 있어요.
    redeploy_runtime_id = ""
    if req.redeploy_record_id:
        rec = _require_owner(req.redeploy_record_id, principal)   # 404/403 게이트
        agent_node = (rec.descriptors or {}).get("agent") or {}
        runtime_arn = agent_node.get("runtimeArn") or ""
        if not runtime_arn:
            raise HTTPException(422, "배포된 agent가 아니라 재배포할 수 없어요.")
        if runtime_arn:
            redeploy_runtime_id = runtime_arn.rsplit("/", 1)[-1]
        meta = inherit_agent_redeploy_metadata(meta, rec)
    try:
        job = svc.create_job(
            SourceRef(req.asset_id, req.version), meta, principal,
            redeploy_runtime_id=redeploy_runtime_id,
            redeploy_record_id=req.redeploy_record_id,
            **({"job_id": job_id} if job_id else {}),
        )
    except SpecCheckError as e:
        raise HTTPException(422, str(e))
    except RuntimeNameConflict as e:
        raise HTTPException(409, str(e))
    except ConcurrentJobAdmission as e:
        raise HTTPException(409, str(e))
    except GateRejected as e:
        raise HTTPException(403, str(e))
    from ..catalog.requests.models import RequestKind
    title = meta.get("name") or req.asset_id.split("/")[-1]
    _log_request(RequestKind.DEPLOY_AGENT,
                 f"{title} 재배포" if req.redeploy_record_id else title,
                 principal, job_id=job.job_id)
    return _agent_deploy_resp(job, svc.deploy_region)


@router.get("/api/agent/name-available")
def agent_name_available(name: str = ""):
    """배포 전 agent runtime 이름 중복 사전 체크(blur 시점).

    available은 기존 계약이고, reason/conflicting_status로 사용자가 볼 수 없는 반려·폐기
    자산이 이름을 점유한 경우를 구분해요. 다른 자산의 상세는 반환하지 않아요.
    """
    from ...shared.deps import get_deploy_service
    svc = get_deploy_service()
    check = svc.agent_name_check(name)
    return {
        "available": check.available,
        "reason": check.reason,
        "conflicting_status": check.conflicting_status,
    }


@router.get("/api/agent/deploy/{job_id}", response_model=AgentDeployResponse)
def agent_deploy_poll(job_id: str):
    """agent 배포 job 상태를 조회하며 한 단계 전진해요(poll-driven advance)."""
    from ...shared.deps import get_deploy_service
    svc = get_deploy_service()
    job = svc.poll(job_id)
    if job is None:
        raise HTTPException(404, "deploy job not found")
    # 배포 완료 governance 후처리. 일반 배포는 기존 auto-scan을 유지하고, 플랫폼이
    # 생성한 Initializr 소스만 security pipeline 없이 직접 승인해 즉시 사용할 수 있게 해요.
    if job.record_id and job.phase is DeployPhase.READY:
        from ..governance.auto_scan import process_deploy_governance
        process_deploy_governance(job)
    return _agent_deploy_resp(job, svc.deploy_region)
