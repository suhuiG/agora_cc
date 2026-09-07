"""Bundle 라우터 — 사용자·관리자 CRUD + 사용자 소비 조회.

생성(POST)은 사용자도 할 수 있고, 만든 사람이 owner가 돼요. 수정·삭제·배포 신청은
소유자 또는 admin만 — `_guard_modify`가 서버에서 강제해요. read(GET)는 공개.
직접 배포(publish)·검토(review)·인계(handoff)는 require_role(*ADMIN_ONLY) 게이트예요.
registry 등 SoT는 BundleService가 shared.deps 경유로만 접근(ADR-004).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from ..catalog.sourcestore.audit import AuditContext
from ..governance.authz import ADMIN_ONLY, require_role

router = APIRouter()


class BundleCreateRequest(BaseModel):
    name: str
    description: str = ""
    member_ids: list[str] = []
    surfaces: list[str] = []
    category: str = ""
    tags: list[str] = []


class BundleUpdateRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    member_ids: list[str] | None = None
    surfaces: list[str] | None = None
    category: str | None = None
    tags: list[str] | None = None


class BundleReviewRequest(BaseModel):
    approved: bool
    note: str = ""


class BundleSummary(BaseModel):
    bundle_id: str
    name: str
    description: str
    member_count: int
    # 타입별 멤버 수 — 리스트 카드가 skill/mcp/agent를 나눠 보여줄 때 사용.
    # members를 확장하지 않는 응답(create/update 직후)에서는 0이에요(클라이언트가 목록 refetch).
    skill_count: int = 0
    mcp_count: int = 0
    agent_count: int = 0
    surfaces: list[str]
    category: str
    tags: list[str]
    created_by: str
    # 그룹을 신청한 사용자와 신청 상태 — 사용자 화면이 자기 신청을 구분해야 해요.
    owner_principal: str = ""
    request_status: str = "NONE"
    updated_at: str


class BundleMember(BaseModel):
    record_id: str
    name: str
    type: str
    status: str
    available: bool


class BundleDetail(BundleSummary):
    members: list[BundleMember]


# DescriptorType.value → BundleSummary 카운트 필드. (SKILL="Agent Skills", MCP="MCP", AGENT="Agent")
_TYPE_COUNT_FIELD = {"Agent Skills": "skill_count", "MCP": "mcp_count", "Agent": "agent_count"}


def _type_counts(members) -> dict[str, int]:
    """확장된 멤버 목록에서 타입별 개수를 집계해요."""
    counts = {"skill_count": 0, "mcp_count": 0, "agent_count": 0}
    for m in members:
        field = _TYPE_COUNT_FIELD.get(m.type)
        if field:
            counts[field] += 1
    return counts


def _summary(b, members=None) -> BundleSummary:
    counts = _type_counts(members) if members is not None else {}
    return BundleSummary(
        bundle_id=b.bundle_id, name=b.name, description=b.description,
        member_count=len(b.member_ids), surfaces=b.surfaces, category=b.category,
        tags=b.tags, created_by=b.created_by, owner_principal=b.owner_principal,
        request_status=b.request_status, updated_at=b.updated_at, **counts,
    )


def _principal(request: Request) -> str:
    return AuditContext.from_request(request).principal()


def _is_admin(request: Request) -> bool:
    from ..identity.context import current_principal
    return bool(set(current_principal(request).roles) & set(ADMIN_ONLY))


def _guard_modify(svc, bundle_id: str, request: Request):
    """그룹을 찾고 수정 권한을 확인해요. 반환: bundle. 실패 시 HTTPException."""
    b = svc.get(bundle_id)
    if b is None:
        raise HTTPException(404, "bundle not found")
    if not svc.can_modify(b, principal=_principal(request),
                          is_admin=_is_admin(request)):
        raise HTTPException(403, "본인이 만든 플러그인만 수정할 수 있어요")
    return b


@router.post("/api/bundles", response_model=BundleSummary)
def create_bundle(req: BundleCreateRequest, request: Request):
    """그룹 생성 — 사용자도 만들 수 있어요. 만든 사람이 owner가 돼요."""
    from ...shared.deps import get_bundle_service
    if not req.name.strip():
        raise HTTPException(422, "name is required")
    principal = _principal(request)
    b = get_bundle_service().create(
        name=req.name, description=req.description, member_ids=req.member_ids,
        surfaces=req.surfaces, category=req.category, tags=req.tags,
        created_by=principal, owner_principal=principal,
    )
    return _summary(b)


@router.get("/api/bundles", response_model=list[BundleSummary])
def list_bundles():
    from ...shared.deps import get_bundle_service
    svc = get_bundle_service()
    return [_summary(b, svc.expand_members(b)) for b in svc.list()]


@router.get("/api/bundles/{bundle_id}", response_model=BundleDetail)
def get_bundle(bundle_id: str):
    from ...shared.deps import get_bundle_service
    svc = get_bundle_service()
    b = svc.get(bundle_id)
    if b is None:
        raise HTTPException(404, "bundle not found")
    expanded = svc.expand_members(b)
    members = [
        BundleMember(record_id=m.record_id, name=m.name, type=m.type,
                     status=m.status, available=m.available)
        for m in expanded
    ]
    base = _summary(b, expanded)
    return BundleDetail(**base.model_dump(), members=members)


@router.patch("/api/bundles/{bundle_id}", response_model=BundleSummary)
def update_bundle(bundle_id: str, req: BundleUpdateRequest, request: Request):
    """그룹 수정 — 소유자 또는 admin만. 남의 그룹은 403이에요."""
    from ...shared.deps import get_bundle_service
    svc = get_bundle_service()
    _guard_modify(svc, bundle_id, request)
    b = svc.update(bundle_id, **req.model_dump(exclude_unset=True))
    if b is None:
        raise HTTPException(404, "bundle not found")
    return _summary(b)


@router.delete("/api/bundles/{bundle_id}")
def delete_bundle(bundle_id: str, request: Request):
    """그룹 삭제 — 소유자 또는 admin만."""
    from ...shared.deps import get_bundle_service, get_publish_service
    svc = get_bundle_service()
    b = _guard_modify(svc, bundle_id, request)
    # 배포된 적 있으면 연결 repo에서 plugin 파일도 함께 지워요(로컬만 지우면 repo에 유령
    # plugin이 남아요). 연결 해제·미배포·삭제 실패는 로컬 삭제를 막지 않아요(best-effort).
    repo_files_deleted = 0
    if b.publish_status == "PUBLISHED":
        try:
            res = get_publish_service().unpublish(b)
            repo_files_deleted = res.files_written
        except Exception:
            repo_files_deleted = 0
    svc.delete(bundle_id)
    return {"deleted": True, "repo_files_deleted": repo_files_deleted}


@router.post("/api/bundles/{bundle_id}/publish",
             dependencies=[Depends(require_role(*ADMIN_ONLY))])
def publish_bundle(bundle_id: str):
    from ...shared.deps import get_bundle_service, get_publish_service
    svc = get_bundle_service()
    b = svc.get(bundle_id)
    if b is None:
        raise HTTPException(404, "bundle not found")
    try:
        res = get_publish_service().publish(b)
    except LookupError as e:
        raise HTTPException(409, str(e))
    except ValueError as e:
        raise HTTPException(422, str(e))
    except RuntimeError as e:
        # git 원격 쪽 실패(권한·API 오류). 성공으로 오보고하면 관리자가 머지할 게
        # 없는 걸 모르게 되니 502로 사유를 그대로 전달해요.
        raise HTTPException(502, str(e))
    svc.mark_published(
        bundle_id, sha=res.commit_sha, version=res.version, at=svc.now(),
        pr_url=res.pr_url)
    return {
        "commit_sha": res.commit_sha, "version": res.version,
        "surfaces": res.surfaces, "skipped": res.skipped,
        "files_written": res.files_written, "pr_url": res.pr_url,
    }


@router.post("/api/bundles/{bundle_id}/request-deploy")
def request_deploy(bundle_id: str, request: Request):
    """사용자 배포 신청. 본인이 만든 그룹만 신청할 수 있어요(admin은 제약 없음)."""
    from ...shared.deps import get_bundle_service
    svc = get_bundle_service()
    _guard_modify(svc, bundle_id, request)
    try:
        b = svc.request_deploy(bundle_id, principal=_principal(request))
    except ValueError as e:
        raise HTTPException(422, str(e))
    if b is None:
        raise HTTPException(404, "bundle not found")
    return {"bundle_id": b.bundle_id, "request_status": b.request_status,
            "owner_principal": b.owner_principal}


@router.post("/api/bundles/{bundle_id}/review",
             dependencies=[Depends(require_role(*ADMIN_ONLY))])
def review_bundle(bundle_id: str, req: BundleReviewRequest, request: Request):
    """관리자 검토. 승인이면 배포까지 이어져요.

    검토 기록과 배포는 분리해요 — repo 미연결·렌더링 실패로 배포가 실패해도 승인
    자체는 남아, 관리자가 repo를 연결한 뒤 재배포할 수 있어요.
    """
    from ...shared.deps import get_bundle_service, get_publish_service
    svc = get_bundle_service()
    b = svc.review(bundle_id, approved=req.approved,
                   reviewer=_principal(request), note=req.note)
    if b is None:
        raise HTTPException(404, "bundle not found")
    if not req.approved:
        return {"request_status": b.request_status, "published": False}

    try:
        res = get_publish_service().publish(b)
    except (LookupError, ValueError, RuntimeError) as e:
        # RuntimeError = git 원격 실패(권한·API). 승인은 남기고 published=false로
        # 사유를 알려요 — 관리자가 권한을 고친 뒤 재배포할 수 있어요.
        return {"request_status": b.request_status, "published": False,
                "error": str(e)}
    svc.mark_published(
        bundle_id, sha=res.commit_sha, version=res.version, at=svc.now(),
        pr_url=res.pr_url)
    return {
        "request_status": b.request_status, "published": True,
        "commit_sha": res.commit_sha, "version": res.version,
        "surfaces": res.surfaces, "pr_url": res.pr_url,
    }


@router.get("/api/bundles/{bundle_id}/handoff",
            dependencies=[Depends(require_role(*ADMIN_ONLY))])
def bundle_handoff(bundle_id: str):
    """관리자 콘솔 인계 안내 — Agora 범위 밖 단계를 알려줘요."""
    from ...shared.deps import get_bundle_service, get_publish_service
    b = get_bundle_service().get(bundle_id)
    if b is None:
        raise HTTPException(404, "bundle not found")
    return get_publish_service().handoff(b)
