"""AwsRegistryAdapter — us-east-1 AWS Agent Registry에 boto3로 위임하는 구현.

control plane(create/lifecycle/폴링)과 data plane(search/curation)이 모두 동작해요.
Registry가 담지 않는 확장메타(owner/tags/category/changelog/search_visible)와
원본 descriptors(tools·markdown 무손실 왕복용, 결정 3)는 로컬 JSON AuxStore에 남기고
get_record/search에서 병합해요. anti-corruption 변환은 aws_mapping에 격리돼 있어요.

AWS 단일화(ADR-012) 이후 이건 유일한 레지스트리 어댑터예요 — shared/deps.py가 항상
이 어댑터로 배선해요(mock/dynamo 폴백은 제거됨). 소스스토어는 서울(ap-northeast-2)의
S3DynamoSourceStore가 담당해 메타=us-east-1 / 실물=서울로 리전이 분리돼요.
근거: docs/03-decisions.md (ADR-011·ADR-012).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from agora.shared.config import registry_endpoint_url, registry_service_names
from agora.shared.gateway_tools import declared_agent_tool_names

from . import aws_mapping as mapping
from .models import (
    AgentMonitoringRecordPage,
    AgentMonitoringRecordProjection,
    DescriptorType,
    RecordAlreadyExists,
    RecordNotFound,
    RecordStatus,
    RegistryRecord,
    SearchHit,
)

if TYPE_CHECKING:  # 타입 힌트용. 런타임 import는 _clients()에서 지연 로딩.
    pass

_NEW_NAMESPACE = "agent-registry"

# boto3 미설치 환경에서 _clients()가 안내하는 메시지예요(ImportError 시).
_PREVIEW_MSG = (
    "AwsRegistryAdapter는 boto3가 필요해요. `pip install boto3` 후 다시 시도하세요."
)

# AWS Agent Registry List/Search API의 maxResults 상한. 초과하면 ValidationException이라
# 이보다 큰 요청(예: 큐 max_results=1000)은 이 크기로 페이지네이션해 모아요.
_AWS_LIST_CAP = 100
_AWS_SEARCH_CAP = 20
_LOG = logging.getLogger(__name__)
_DEPLOYED_AGENT_INCOMPLETE = (
    "일부 agent의 배포 상태를 확인하지 못해 목록이 불완전할 수 있어요."
)


def _is_conflict(error: Exception) -> bool:
    """이 boto 예외가 "이미 있음"(ConflictException)인가.

    error code를 먼저 봐요 — 메시지 문구는 AWS가 바꿀 수 있으니까요. 클래스 이름과 메시지
    검사는 폴백이에요(botocore errorfactory가 만드는 동적 클래스, 테스트 fake 모두 커버).
    """
    code = getattr(error, "response", {}).get("Error", {}).get("Code", "")
    if code == "ConflictException":
        return True
    if "ConflictException" in type(error).__name__:
        return True
    return "already exists" in str(error)


class AwsRegistryAdapter:
    """RegistryPort의 AWS 구현 (라이브).

    us-east-1 AWS Agent Registry에 boto3로 위임해요. control plane
    (create/lifecycle/폴링)은 bedrock-agentcore-control, data plane(search)은
    bedrock-agentcore 클라이언트를 써요. 확장메타·원본 descriptors는 AuxStore에 병합해요.

    Args:
        region: AgentCore GA 리전 (예: us-east-1).
        control_client / data_client: 테스트·DI용 주입 포인트. None이면 지연 생성 시도.
    """

    def __init__(
        self,
        *,
        region: str = "us-east-1",
        registry_id: str = "",
        namespace: str = "agent-registry",
        aux=None,
        control_client=None,
        data_client=None,
        sleep=None,
    ) -> None:
        import time as _time
        self.region = region
        self.registry_id = registry_id
        self.namespace = namespace
        self._aux = aux
        self._control = control_client
        self._data = data_client
        self._sleep = sleep or _time.sleep

    @property
    def _is_new_namespace(self) -> bool:
        return self.namespace == _NEW_NAMESPACE

    def _status_filter_kwargs(self, status_value: str) -> dict:
        """ListRegistryRecords의 status 필터 — 구=`status=`, 신=구조화 `filters=[{name,values}]`. (CA-05)"""
        if self._is_new_namespace:
            return {"filters": [{"name": "status", "values": [status_value]}]}
        return {"status": status_value}

    # ── 지연 클라이언트 생성 ──────────────────────────────────────────
    def _clients(self):
        if self._control is None or self._data is None:
            try:
                import boto3  # noqa: PLC0415 (지연 import 의도)
            except ImportError as e:  # pragma: no cover - 환경 의존
                raise RuntimeError(_PREVIEW_MSG) from e
            # 라이브 control/data plane 클라이언트. control은 create/lifecycle,
            # data는 search를 담당해요(둘 다 us-east-1). 네임스페이스(CA-05)에 따라
            # 서비스명·endpoint가 달라요 — agent-registry는 `.api.aws` endpoint를
            # 명시적으로 넘겨야 올바른 호스트에 도달해요(botocore 기본 해석은 .amazonaws.com).
            ctrl_svc, data_svc = registry_service_names(self.namespace)
            self._control = self._control or boto3.client(
                ctrl_svc, region_name=self.region,
                endpoint_url=registry_endpoint_url(self.namespace, ctrl_svc, self.region),
            )
            self._data = self._data or boto3.client(
                data_svc, region_name=self.region,
                endpoint_url=registry_endpoint_url(self.namespace, data_svc, self.region),
            )
        return self._control, self._data

    # ── 관리 (control plane) ─────────────────────────────────────────
    def create_registry(self, name: str, description: str = "") -> str:
        ctrl, _ = self._clients()
        for reg in ctrl.list_registries().get("registries", []):
            if reg["name"] == name:
                return reg["registryArn"]
        # approvalConfiguration: Agora는 자체 승인 게이트(등록 hook → PENDING_APPROVAL →
        # 스캔 → 관리자 승인)로 레코드 상태를 제어해요. registry 레벨 자동승인은 이 게이트를
        # 우회하므로 신 네임스페이스에선 꺼요. ["APPROVE_ALL"]은 등록 즉시 APPROVED로 만들어
        # 스캔·관리자 승인을 건너뛰는 버그였어요(CA-05, ADR-0015 매핑 정정) — 빈
        # autoApprovalRules로 비활성화해요. 구 네임스페이스(deprecated)는 검증된 기존 동작을
        # 유지해요(autoApproval:true여도 create_record는 DRAFT, Agora 게이트가 승인을 제어).
        approval = (
            {"autoApprovalRules": []}
            if self._is_new_namespace else {"autoApproval": True}
        )
        # agent-registry(신 네임스페이스)는 description이 필수(min length 1)라 빈 문자열을
        # 거부해요(구 네임스페이스는 허용). 비어 있으면 name으로 채워 양쪽 모두 안전하게. (CA-05)
        resp = ctrl.create_registry(
            name=name, description=description or name, approvalConfiguration=approval,
        )
        return resp["registryArn"]

    def create_record(
        self, registry_id, name, descriptor_type, descriptors, record_version,
        *, description="", owner_team="", owner_user="", owner_contact="",
        tags=(), category="", changelog="", escalation_contact="",
    ) -> RegistryRecord:
        ctrl, _ = self._clients()
        aws_desc = mapping.to_registry_descriptors(
            descriptor_type, descriptors, name=name, description=description,
            namespace=self.namespace,
        )
        # description은 Registry 코어 필드로 함께 보내요(CreateRegistryRecord가 지원).
        # 이래야 GetRegistryRecord가 top-level description을 돌려줘 상세 화면에 표시돼요.
        # (SKILL은 skillMd frontmatter에도 들어가지만, 그건 검증용이라 코어에 별도로 저장.)
        # 타입 필드: 구=descriptorType(A2A/AGENT_SKILLS/…), 신=recordType(AGENT/SKILL/…).
        # 신 네임스페이스는 name(dedup 키)·displayName(표시명)이 분리 필수라, name엔 slug를
        # 넣고 표시명은 displayName으로 보내요. 구는 name이 곧 표시명. (CA-05)
        if self._is_new_namespace:
            create_kwargs = dict(
                registryId=registry_id, name=mapping.dedup_name(name), displayName=name,
                recordType=mapping.to_record_type(descriptor_type),
                descriptors=aws_desc, recordVersion=record_version,
            )
        else:
            create_kwargs = dict(
                registryId=registry_id, name=name,
                descriptorType=mapping.to_registry_descriptor_type(descriptor_type),
                descriptors=aws_desc, recordVersion=record_version,
            )
        if description:
            create_kwargs["description"] = description
        try:
            resp = ctrl.create_registry_record(**create_kwargs)
        except Exception as error:
            # ConflictException = 같은 이름·버전이 이미 있음. 도메인 예외로 번역해요 —
            # 그러지 않으면 등록 라우터가 처리되지 않은 500으로 떨어져요(CA-33).
            if _is_conflict(error):
                raise RecordAlreadyExists(name, record_version) from error
            raise
        record_id = mapping.record_id_from_arn(resp["recordArn"])
        # CREATING → DRAFT 폴링 (실측: 비동기, ~2초)
        self._wait_until_ready(registry_id, record_id)
        # 확장메타 + 원본 descriptors를 Aux에 저장(B안: 로컬 JSON). Aux 미주입이면 생략.
        # descriptors 원본 저장 이유: Registry엔 mcp.server만 보내므로(tools 제외),
        # MCP 상세 tool 목록·skill markdown을 무손실 왕복하려면 원본을 Aux가 보관해야 함(결정 3).
        # description도 Aux에 함께 저장해 Registry 코어가 비어 오는 경우의 폴백으로 써요.
        if self._aux is not None:
            self._aux.set_meta(
                record_id, owner_team=owner_team, owner_user=owner_user,
                owner_contact=owner_contact,
                tags=tuple(tags), category=category, changelog=changelog,
                escalation_contact=escalation_contact,
                description=description, descriptors=dict(descriptors),
            )
        return self.get_record(registry_id, record_id)

    @staticmethod
    def _update_record_shape(ctrl, member: str):
        """UpdateRegistryRecord 입력 shape의 멤버를 돌려줘요. 못 찾으면 None.

        테스트 fake 클라이언트엔 service_model이 없어서 None으로 떨어지고,
        그때는 wrap_optional_values가 원본을 그대로 통과시켜요.
        """
        try:
            model = ctrl.meta.service_model.operation_model("UpdateRegistryRecord")
            return model.input_shape.members.get(member)
        except Exception:
            return None

    def update_record_descriptors(
        self, registry_id, record_id, name, descriptor_type, descriptors, record_version,
        *, description="",
    ) -> RegistryRecord:
        """기존 레코드의 descriptors·version을 갱신해요 (재배포용).

        새 레코드를 만들지 않아서 리뷰·조회수·번들 멤버십이 그대로 살아요.
        Aux의 원본 descriptors도 같이 갱신해야 상세 화면이 새 값을 보여줘요
        (Aux가 원본 보관처라 여기만 빠지면 화면이 옛 값을 계속 보여줘요).
        """
        ctrl, _ = self._clients()
        aws_desc = mapping.to_registry_descriptors(
            descriptor_type, descriptors, name=name, description=description,
            namespace=self.namespace,
        )
        # UpdateRegistryRecord는 partial update를 표현하려고 갱신 가능한 필드마다
        # {"optionalValue": ...} 래퍼를 둬요 — Create와 형태가 다르고 중첩 레벨마다
        # 반복돼요(실측 2026-07-27: 안 감싸면 'Unknown parameter in descriptors: "a2a"',
        # 최상위만 감싸면 '...descriptors.optionalValue.a2a: "agentCard"'로 거부).
        # 래퍼 위치는 botocore shape에서 읽어요(하드코딩하면 스키마 변경에 조용히 깨져요).
        # 타입 필드는 둘 다 plain(비래핑): 구=descriptorType, 신=recordType. (CA-05)
        update_kwargs = dict(
            registryId=registry_id, recordId=record_id,
            descriptors=mapping.wrap_optional_values(
                aws_desc, self._update_record_shape(ctrl, "descriptors")),
            recordVersion=record_version,
        )
        if self._is_new_namespace:
            update_kwargs["recordType"] = mapping.to_record_type(descriptor_type)
        else:
            update_kwargs["descriptorType"] = mapping.to_registry_descriptor_type(
                descriptor_type)
        if description:
            update_kwargs["description"] = {"optionalValue": description}
        ctrl.update_registry_record(**update_kwargs)
        self._wait_until_ready(registry_id, record_id)
        if self._aux is not None:
            self._aux.set_meta(record_id, descriptors=dict(descriptors))
        return self.get_record(registry_id, record_id)

    # 레코드가 전이 중임을 뜻하는 상태 — 이게 끝날 때까지 기다려요.
    # UPDATING은 update_registry_record 직후에 나와요(실측 2026-07-27: 재배포에서
    # 이걸 빼먹어 RecordStatus('UPDATING') ValueError로 카탈로그 등재가 죽었어요).
    _TRANSIENT_STATUSES = ("CREATING", "UPDATING")

    def _wait_until_ready(self, registry_id: str, record_id: str) -> str:
        ctrl, _ = self._clients()
        status = "CREATING"
        for _ in range(10):
            item = ctrl.get_registry_record(registryId=registry_id, recordId=record_id)
            status = item["status"]
            if status not in self._TRANSIENT_STATUSES:
                return status
            self._sleep(0.5)
        return status

    def submit_for_approval(self, registry_id, record_id) -> RecordStatus:
        ctrl, _ = self._clients()
        resp = ctrl.submit_registry_record_for_approval(
            registryId=registry_id, recordId=record_id)
        return RecordStatus(resp["status"])

    def update_status(self, registry_id, record_id, status, reason="") -> RecordStatus:
        # 실 AgentCore 제약(실배포 e2e 확인): DRAFT→PENDING_APPROVAL 전이는 오직 전용 API
        # submit_registry_record_for_approval 로만 가능해요. update_registry_record_status(
        # status="PENDING_APPROVAL") 는 `ValidationException: Invalid target status:
        # PENDING_APPROVAL` 로 거부돼요. 그래서 target 이 PENDING_APPROVAL 이면 submit 으로
        # 라우팅해요 — 이 한 곳 수정으로 run_async / _ensure_pending_before_verdict / _decide
        # 3개 call site 의 DRAFT→PENDING 전이가 전부 실제로 성사돼요(예전엔 try/except 로 삼켜져
        # 자산이 DRAFT 에 고립 → catalog(APPROVED 만 노출) 공백). 다른 상태는 기존 경로 유지.
        if status is RecordStatus.PENDING_APPROVAL:
            return self.submit_for_approval(registry_id, record_id)
        ctrl, _ = self._clients()
        resp = ctrl.update_registry_record_status(
            registryId=registry_id, recordId=record_id,
            status=status.value, statusReason=reason or "updated")
        return RecordStatus(resp["status"])

    def get_record(self, registry_id, record_id) -> RegistryRecord:
        ctrl, _ = self._clients()
        try:
            item = ctrl.get_registry_record(registryId=registry_id, recordId=record_id)
        except Exception as e:  # boto ResourceNotFound 등
            error_code = getattr(e, "response", {}).get("Error", {}).get("Code", "")
            if (
                error_code in {"ResourceNotFoundException", "ValidationException"}
                or "NotFound" in type(e).__name__
                or "ResourceNotFound" in str(e)
            ):
                raise RecordNotFound(f"{registry_id}/{record_id}") from e
            raise
        record = mapping.to_record(item, registry_id=registry_id)
        return self._merge_aux(record)

    def _merge_aux(self, record: RegistryRecord) -> RegistryRecord:
        """확장메타(owner/tags/category 등)를 Aux에서 읽어 병합. Aux 없으면 그대로."""
        if self._aux is None:
            return record
        return self._aux.merge_into(record)

    def _agent_monitoring_projection(
        self,
        record: RegistryRecord,
    ) -> AgentMonitoringRecordProjection:
        ext = (
            self._aux.get_agent_monitoring_ext(record.record_id)
            if self._aux is not None
            else {
                "owner_team": "",
                "owner_user": "",
                "agent_declaration": None,
            }
        )
        registry_agent = (
            record.descriptors.get("agent")
            if isinstance(record.descriptors, dict)
            else None
        )
        registry_agent = (
            registry_agent if isinstance(registry_agent, dict) else {}
        )
        ext_declaration = ext["agent_declaration"]
        agent_declaration = (
            {
                **(ext_declaration or {}),
                **{
                    key: registry_agent[key]
                    for key in (
                        "agoraDependencies",
                        "builtinTools",
                        "sourcePrefix",
                    )
                    if key in registry_agent
                },
            }
            if ext_declaration is not None or registry_agent
            else None
        )
        return AgentMonitoringRecordProjection(
            record_id=record.record_id,
            name=record.name,
            version=record.version,
            status=record.status,
            owner_team=ext["owner_team"],
            owner_user=ext["owner_user"],
            updated_at=record.updated_at,
            has_source=bool(
                agent_declaration and agent_declaration.get("sourcePrefix")
            ),
            declared_tool_names=(
                declared_agent_tool_names({"agent": agent_declaration})
                if agent_declaration is not None
                else None
            ),
        )

    def get_agent_monitoring_record(
        self,
        registry_id: str,
        record_id: str,
    ) -> AgentMonitoringRecordProjection:
        """Read one Agent without merging descriptor bodies from Aux."""
        ctrl, _ = self._clients()
        try:
            item = ctrl.get_registry_record(
                registryId=registry_id,
                recordId=record_id,
            )
        except Exception as error:
            error_code = getattr(error, "response", {}).get(
                "Error", {}
            ).get("Code", "")
            if (
                error_code
                in {"ResourceNotFoundException", "ValidationException"}
                or "NotFound" in type(error).__name__
                or "ResourceNotFound" in str(error)
            ):
                raise RecordNotFound(
                    f"{registry_id}/{record_id}"
                ) from error
            raise
        record = mapping.to_record(item, registry_id=registry_id)
        if record.descriptor_type is not DescriptorType.AGENT:
            raise RecordNotFound(f"{registry_id}/{record_id}")
        return self._agent_monitoring_projection(record)

    def list_agent_monitoring_records(
        self,
        registry_id: str,
    ) -> AgentMonitoringRecordPage:
        """Exhaust Registry pages and emit body-free Agent projections."""
        ctrl, _ = self._clients()
        target_statuses = (
            RecordStatus.DRAFT,
            RecordStatus.PENDING_APPROVAL,
            RecordStatus.APPROVED,
            RecordStatus.REJECTED,
        )
        records: list[AgentMonitoringRecordProjection] = []
        for status in target_statuses:
            next_token = None
            while True:
                kwargs = {
                    "registryId": registry_id,
                    **self._status_filter_kwargs(status.value),
                    "maxResults": _AWS_LIST_CAP,
                }
                if next_token:
                    kwargs["nextToken"] = next_token
                response = ctrl.list_registry_records(**kwargs)
                for item in response.get("registryRecords", []):
                    record = mapping.to_record(item, registry_id=registry_id)
                    if record.descriptor_type is DescriptorType.AGENT:
                        records.append(
                            self._agent_monitoring_projection(record)
                        )
                next_token = response.get("nextToken")
                if not next_token:
                    break
        return AgentMonitoringRecordPage(
            records=tuple(records),
            truncated=False,
            observed_population=len(records),
        )

    # ── 발견 (data plane) ────────────────────────────────────────────
    def search(
        self,
        registry_ids: list[str],
        query: str,
        *,
        max_results: int = 10,
        descriptor_type: DescriptorType | None = None,
    ) -> list[SearchHit]:
        ctrl, data = self._clients()
        # 빈 쿼리 = "전체 목록"(카탈로그 둘러보기). 실제 data-plane search는 searchQuery
        # 최소 1자를 요구하므로(ParamValidationError), 빈 쿼리는 control-plane
        # ListRegistryRecords(status=APPROVED)로 전환해요. mock/dynamo의 "빈 쿼리=전부" 계약 유지.
        if query.strip():
            # 데이터 플레인 search: 구=SearchRegistryRecords, 신=SearchDiscoverableRegistryRecords.
            # 둘 다 상한이 20이고 nextToken이 없어요. 큰 내부 요청도 AWS ValidationException 대신
            # 지원 가능한 최대 페이지를 반환하게 방어합니다. (CA-05)
            search_op = (
                data.search_discoverable_registry_records
                if self._is_new_namespace else data.search_registry_records
            )
            items = search_op(
                searchQuery=query,
                registryIds=registry_ids,
                maxResults=min(max_results, _AWS_SEARCH_CAP),
            ).get("registryRecords", [])
        else:
            # ListRegistryRecords maxResults 상한(_AWS_LIST_CAP)을 초과하면 ValidationException.
            # 호출부가 큰 값(예: 큐 1000)을 줘도 안전하게, 상한 페이지로 나눠 max_results까지 모아요.
            items = []
            next_token = None
            while len(items) < max_results:
                kwargs = {"registryId": registry_ids[0],
                          **self._status_filter_kwargs("APPROVED"),
                          "maxResults": min(max_results - len(items), _AWS_LIST_CAP)}
                if next_token:
                    kwargs["nextToken"] = next_token
                resp = ctrl.list_registry_records(**kwargs)
                items.extend(resp.get("registryRecords", []))
                next_token = resp.get("nextToken")
                if not next_token:
                    break
        from dataclasses import replace as _replace

        from .models import endpoint_of_descriptors, source_prefix_of_descriptors

        hits = []
        for item in items:
            hit = mapping.to_hit(item, registry_id=registry_ids[0])
            # APPROVED만 노출 (mock/dynamo 어댑터와 동일한 방어선).
            # AWS API가 혹시 non-APPROVED를 돌려줘도 여기서 걸러요(defense-in-depth).
            if not hit.status.is_discoverable:
                continue
            if descriptor_type and hit.descriptor_type is not descriptor_type:
                continue
            # 확장 메타(owner/tags/category/source_prefix)를 Aux에서 hit에 병합해요.
            # 이래야 목록 조회가 자산마다 get_record를 다시 부르지 않아요(N+1 회피).
            if self._aux is not None:
                ext = self._aux.get_ext(hit.record_id)
                if not ext["search_visible"]:
                    continue
                descriptors = ext["descriptors"] or {}
                agent = descriptors.get("agent") if isinstance(descriptors, dict) else None
                deployment_kind = ""
                if isinstance(agent, dict) and agent.get("runtimeArn"):
                    deployment_kind = "runtime"
                hit = _replace(
                    hit,
                    owner_team=ext["owner_team"], owner_user=ext["owner_user"],
                    tags=ext["tags"], category=ext["category"],
                    source_prefix=source_prefix_of_descriptors(descriptors),
                    endpoint=endpoint_of_descriptors(descriptors),
                    deployment_kind=deployment_kind,
                )
            hits.append(hit)
        return hits[:max_results]

    def list_deployed_agent_hits(
        self,
        registry_id: str,
        *,
        name: str = "",
        page_size: int = 20,
        cursor: str | None = None,
    ):
        """Control-plane cursor로 배포 Agent를 이름 필터링해 반환해요."""
        import json
        from dataclasses import replace as _replace

        from .models import SearchHitPage

        if self._aux is None:
            raise RuntimeError(
                "Playground deployed-agent projection requires AuxStore",
            )
        ctrl, _ = self._clients()
        query = name.casefold().strip()
        scan_page_size = min(_AWS_LIST_CAP, max(page_size * 2, 20))
        try:
            state = json.loads(cursor) if cursor else {"token": None, "skip": 0}
            if (
                state.keys() != {"token", "skip"}
                or (
                    state["token"] is not None
                    and (
                        not isinstance(state["token"], str)
                        or len(state["token"]) > 2048
                    )
                )
                or not isinstance(state["skip"], int)
                or state["skip"] < 0
                or state["skip"] >= scan_page_size
            ):
                raise ValueError
        except Exception as exc:
            raise ValueError("invalid deployed agent cursor") from exc
        next_token = state["token"]
        skip = state["skip"]
        hits = []
        incomplete_reason = ""
        # 희소한 배포 agent 때문에 한 요청이 무한 scan이 되지 않게 페이지 수를 제한해요.
        # 결과가 비어도 next cursor를 주므로 클라이언트가 명시적으로 계속 탐색할 수 있어요.
        for _ in range(5):
            kwargs = {
                "registryId": registry_id,
                **self._status_filter_kwargs("APPROVED"),
                "maxResults": scan_page_size,
            }
            if next_token:
                kwargs["nextToken"] = next_token
            response = ctrl.list_registry_records(**kwargs)
            records = response.get("registryRecords", [])
            for index, item in enumerate(records[skip:], start=skip):
                hit = mapping.to_hit(item, registry_id=registry_id)
                if hit.descriptor_type is not DescriptorType.AGENT:
                    continue
                if query and query not in hit.name.casefold():
                    continue
                ext = self._aux.get_ext(hit.record_id)
                if not ext["search_visible"]:
                    continue
                descriptors = ext["descriptors"]
                agent = (
                    descriptors.get("agent")
                    if isinstance(descriptors, dict)
                    else None
                )
                deployment_judged = (
                    isinstance(agent, dict)
                    and (
                        bool(agent.get("runtimeArn"))
                        or "executionBinding" in agent
                    )
                )
                if not deployment_judged:
                    try:
                        record = self.get_record(registry_id, hit.record_id)
                    except Exception:
                        _LOG.warning(
                            "Unable to determine deployment state for registry record %s",
                            hit.record_id,
                            exc_info=True,
                        )
                        incomplete_reason = _DEPLOYED_AGENT_INCOMPLETE
                        continue
                    descriptors = record.descriptors
                    agent = (
                        descriptors.get("agent")
                        if isinstance(descriptors, dict)
                        else None
                    )
                deployment_kind = ""
                if isinstance(agent, dict) and agent.get("runtimeArn"):
                    deployment_kind = "runtime"
                if not deployment_kind:
                    continue
                hits.append(_replace(hit, deployment_kind=deployment_kind))
                if len(hits) >= page_size:
                    if index + 1 < len(records):
                        following = json.dumps(
                            {"token": next_token, "skip": index + 1},
                            separators=(",", ":"),
                        )
                    else:
                        token = response.get("nextToken")
                        following = (
                            json.dumps(
                                {"token": token, "skip": 0},
                                separators=(",", ":"),
                            )
                            if token
                            else None
                        )
                    return SearchHitPage(
                        items=tuple(hits),
                        next_cursor=following,
                        incomplete_reason=incomplete_reason,
                    )
            next_token = response.get("nextToken")
            skip = 0
            if not next_token:
                break
        following = (
            json.dumps(
                {"token": next_token, "skip": 0},
                separators=(",", ":"),
            )
            if next_token
            else None
        )
        return SearchHitPage(
            items=tuple(hits),
            next_cursor=following,
            incomplete_reason=incomplete_reason,
        )

    def find_by_source_asset_id(
        self, registry_id: str, asset_id: str
    ) -> list[RegistryRecord]:
        # MVP: Registry는 sourcePrefix를 저장하지 않아 소스 기반 조회 불가.
        # 형제 버전 처리(§12)는 Task 6 범위 밖 — 계약상 빈 리스트 허용.
        return []

    def list_records(
        self, registry_id, *, statuses=None, max_results=1000,
    ) -> list[RegistryRecord]:
        ctrl, _ = self._clients()
        # AWS ListRegistryRecords는 status별 필터를 받아요. statuses 미지정이면 큐가
        # 봐야 할 전 상태를 순회해요(APPROVED만 주는 search와 달리 심사 대상 전체).
        from .models import RecordStatus as _RS
        target_statuses = statuses or (
            _RS.DRAFT, _RS.PENDING_APPROVAL, _RS.APPROVED, _RS.REJECTED)
        out: list[RegistryRecord] = []
        for st in target_statuses:
            next_token = None
            while max_results is None or len(out) < max_results:
                page_size = (
                    _AWS_LIST_CAP
                    if max_results is None
                    else min(max_results - len(out), _AWS_LIST_CAP)
                )
                kwargs = {"registryId": registry_id,
                          **self._status_filter_kwargs(st.value),
                          "maxResults": page_size}
                if next_token:
                    kwargs["nextToken"] = next_token
                resp = ctrl.list_registry_records(**kwargs)
                for item in resp.get("registryRecords", []):
                    rec = mapping.to_record(item, registry_id=registry_id)
                    out.append(self._merge_aux(rec))
                next_token = resp.get("nextToken")
                if not next_token:
                    break
        return out if max_results is None else out[:max_results]

    def delete_record(self, registry_id, record_id) -> None:
        ctrl, _ = self._clients()
        try:
            ctrl.delete_registry_record(registryId=registry_id, recordId=record_id)
        except Exception as e:
            if "NotFound" in type(e).__name__ or "ResourceNotFound" in str(e):
                raise RecordNotFound(f"{registry_id}/{record_id}") from e
            raise
        # 확장 메타(owner/tags/descriptors 등) 잔재 정리 — Aux 있으면.
        if self._aux is not None and hasattr(self._aux, "purge_record"):
            self._aux.purge_record(record_id)

    def set_search_visible(
        self, registry_id: str, record_id: str, visible: bool
    ) -> RegistryRecord:
        if self._aux is not None:
            self._aux.set_meta(record_id, search_visible=visible)
        return self.get_record(registry_id, record_id)

    def update_curation(
        self,
        registry_id: str,
        record_id: str,
        *,
        description: str | None = None,
        tags: tuple[str, ...] | None = None,
        category: str | None = None,
        changelog: str | None = None,
    ) -> RegistryRecord:
        # description은 Registry 코어라 update_registry_record로,
        # tags/category/changelog는 Aux로. MVP: description도 Aux 보류(코어 갱신은 Task 6).
        if self._aux is not None:
            fields = {}
            if tags is not None:
                fields["tags"] = tags
            if category is not None:
                fields["category"] = category
            if changelog is not None:
                fields["changelog"] = changelog
            if fields:
                self._aux.set_meta(record_id, **fields)
        return self.get_record(registry_id, record_id)
