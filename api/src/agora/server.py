"""Agora API 서버 — FastAPI 앱 조립점.

도메인 라우터를 include 하기만 해요. 실제 핸들러는 각 도메인 패키지에 있어요:
  - 카탈로그 도메인 : domains/catalog/router.py  (발견·상세·리뷰·퍼블리시·소스)
  - 거버넌스 도메인 : domains/governance/router.py (승인 워크플로우)

새 도메인을 추가할 때: domains/<도메인>/router.py 에 APIRouter 를 만들고, 아래에
include_router 한 줄을 더해요. (가이드: domains/README.md)

DI(어댑터·스토어)는 shared/deps.py 가 소유해요. 테스트/배포는 set_source_store 로 주입.

실행: uvicorn agora.server:app --reload --port 9100
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .domains.bundle.router import router as bundle_router
from .domains.catalog.mcp.drift_router import router as mcp_drift_router
from .domains.catalog.publish.router import router as publish_router
from .domains.catalog.router import router as catalog_router
from .domains.governance.router import router as governance_router
from .domains.identity.access_router import (
    internal_router as identity_internal_router,
    router as identity_access_router,
)
from .domains.identity.middleware import IdentityMiddleware
from .domains.identity.dev_identity_router import router as dev_identity_router
from .domains.identity.domain_policy_router import (
    router as identity_domain_policy_router,
)
from .domains.identity.inventory_gate_router import (
    router as identity_inventory_gate_router,
)
from .domains.identity.router import router as identity_router
from .domains.identity.directory_router import router as identity_directory_router
from .domains.identity.users_router import router as identity_users_router
from .domains.monitoring.router import router as monitoring_router
from .domains.playground.router import router as playground_router
from .domains.runtime.router import router as runtime_router
# 임시(GA 때 제거) — 로컬 폴러 ON/OFF.
from .domains.runtime.dev_poller_router import router as dev_poller_router
from .shared.deps import (
    set_dev_identity_retry_actor_available,
    set_source_store,  # noqa: F401 (공개 주입점)
)

_log = logging.getLogger(__name__)


def _ensure_agora_log_visibility() -> None:
    """`agora.*` 의 INFO 를 실제로 프로덕션 로그에 내보내요.

    **왜 필요한가 (2026-09-05 라이브 실측).** 컨테이너는 `uvicorn agora.server:app` 로 뜨고
    (`api/Dockerfile`), uvicorn 은 **자기 로거만**(`uvicorn`·`uvicorn.error`·`uvicorn.access`)
    설정해요. 저장소에는 애플리케이션 로깅 설정이 **한 곳도 없어요**. 그래서 root 로거가
    기본 `WARNING` 이고 handler 도 없는 상태였고, `agora.*` 의

    - `WARNING` 이상은 `logging.lastResort` 를 타고 **레벨 접두어 없이** stderr 로 나가고
    - `INFO` 는 **전부 버려졌어요** (`_log.info` 호출부 전체)

    IH-160 배포 검증에서 이게 드러났어요 — `_shared_resource_cleanup_owner()` 의 양성 분기
    로그를 `INFO` 로 넣었는데 라이브 로그에 **양쪽 분기가 다 안 보였어요.** 원인을 「가드가
    안 돌았다」로 오진할 수 있는 상태였어요. 실제로는 td `:43` 이 정상 기동한 상태였어요.

    **왜 root 가 아니라 `agora` 로거인가.** root 를 건드리면 의존 라이브러리의 INFO 까지
    쏟아지고 uvicorn 설정과 겹쳐요. `agora` 하나에만 handler 를 달아요.

    **`propagate` 를 끄지 않아요.** 끄면 pytest `caplog`(root handler + 전파로 잡아요)가
    `agora.*` 를 못 잡아서 기존 테스트 다수가 조용히 죽어요.

    ⚠️ **「handler 가 있다」로 판정하면 안 돼요 (2026-09-05 적대적 리뷰가 재현).** 초판은
    `if not root.handlers` 였는데, 다른 bootstrap 이 `NullHandler` 를 먼저 붙여 두면 그 가드가
    참이 되어 StreamHandler 를 **안 달아요.** 그런데 `logging.lastResort` 는 계층을 걷다 handler
    를 하나라도 찾으면(`found > 0`) 발동하지 않으므로, INFO 는 물론 **WARNING 까지 조용히
    사라져요.** 별 프로세스에서 재현했을 때 level·handlers·`isEnabledFor`·`propagate` 네 단정이
    전부 참인데 stderr 는 빈 문자열이었어요 — 「handler 객체의 존재」는 「레코드가 나간다」가
    아니에요. 그래서 **실제로 내보내는 handler(`emit` 가 stream 으로 가는 것)** 가 있는지 봐요.
    """
    root = logging.getLogger("agora")
    if root.level == logging.NOTSET or root.level > logging.INFO:
        root.setLevel(logging.INFO)
    if not any(_handler_emits(handler) for handler in root.handlers):
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter("%(levelname)s %(name)s: %(message)s")
        )
        root.addHandler(handler)


def _handler_emits(handler: logging.Handler) -> bool:
    """이 handler 가 INFO 레코드를 실제로 «어딘가로 내보내나».

    `NullHandler` 는 `emit` 가 `pass` 예요 — 존재하지만 아무것도 내보내지 않아요. handler 자체의
    level 이 INFO 보다 높으면 그것도 안 나가요. 둘 다 「있다」로 세면 안 돼요.
    """
    if isinstance(handler, logging.NullHandler):
        return False
    if handler.level > logging.INFO:
        return False
    return True


_ensure_agora_log_visibility()


def _poller_enabled() -> bool:
    """Role별 폴러 소유권을 검증하고 기동 여부를 반환해요."""
    role = os.getenv("AGORA_ROLE", "").lower()
    enabled = os.getenv("AGORA_POLLER_ENABLED", "").lower() in ("1", "true", "yes")
    forced = os.getenv("AGORA_POLLER_FORCE", "").lower() in ("1", "true", "yes")
    if role == "lambda":
        if enabled:
            raise RuntimeError(
                "AGORA_ROLE=lambda cannot run the deployment poller; "
                "set AGORA_POLLER_ENABLED=0"
            )
        return False
    if role == "portal":
        if not enabled:
            raise RuntimeError(
                "AGORA_ROLE=portal requires AGORA_POLLER_ENABLED=1; "
                "otherwise deployments remain QUEUED"
            )
        return True
    if role == "local" and enabled:
        if not forced:
            raise RuntimeError(
                "AGORA_ROLE=local cannot enable the shared deployment poller; "
                "set AGORA_POLLER_ENABLED=0 or explicitly set AGORA_POLLER_FORCE=1"
            )
        _log.warning(
            "AGORA_POLLER_FORCE=1: local backend is polling shared dev resources "
            "together with the portal"
        )
        return True
    return False


def _shared_resource_cleanup_owner() -> bool:
    """이 프로세스가 **공유 Gateway 자원의 파괴적 정리를 소유**하나요.

    답은 role 하나예요 — `AGORA_ROLE == portal`.

    ## 왜 테이블 이름으로 판정하지 않나 (2026-09-05 라이브 실측)

    예전 판정은 `config.identity_table == f"agora-identity-{config.stage}"` 였어요. 그런데
    **라이브 포털은 `AGORA_STAGE=prod` 인데 `AGORA_IDENTITY_TABLE=agora-identity-dev`** 예요 —
    stage 와 데이터 테이블 이름이 의도적으로 분리돼 있어요. 그래서 기대값이
    `agora-identity-prod` 로 계산되어 그 판정이 **항상 거짓**이었고, dev identity sweeper 가
    프로덕션에서 조용히 꺼져 있었어요(라이브 로그로 확인). 만료된 dev 크리덴셜 정리가 한 번도
    안 돌았다는 뜻이고, 그게 ADR-0108 이 막으려던 IH-157 잼 전조를 계속 쌓아요.

    이름 규약으로 「내가 소유자인가」를 추론하려 한 게 오류였어요. 막으려던 실제 위협은
    **개발자 노트북이 공유 자원을 지우는 것**이고, 그건 role 이 정확히 표현해요. 문서화된 로컬
    실 AWS 절차는 jobs 테이블만 갈라 두고 identity·registry·라이브 Gateway 는 공유하니
    (AGENTS.md), 테이블 이름으로는 그 둘을 구별할 수 없어요.

    **대가**: 로컬 백엔드에서는 만료 크리덴셜 정리와 IH-160 수렴 재시도가 돌지 않아요. 그건
    의도예요 — 그 두 동작은 공유 Gateway 정책을 건드려요. 로컬에서 필요하면 어드민 조작이
    같은 provisioning 을 걸어요.
    """
    role = os.getenv("AGORA_ROLE", "").lower()
    if role != "portal":
        # ⚠️ 레벨이 관측 계약이에요 — `WARNING` 이어야 해요. 옛 테이블 정렬 가드도 `WARNING`
        # 이었고(`66ac97a4`), 그래서 2026-09-05 에 라이브 로그로 「프로덕션에서 한 번도 안
        # 돌았다」를 잡을 수 있었어요. `31fb2ace` 가 role 판정으로 통일하면서 이 줄을 `INFO` 로
        # 내렸는데, 그 시점 프로덕션은 `agora.*` INFO 를 전부 버리고 있어서 **그 진단 자체가
        # 사라졌어요.** 파괴적 정리가 꺼졌다는 사실은 정상 상태가 아니니 `WARNING` 이 맞아요.
        _log.warning(
            "shared resource cleanup disabled: AGORA_ROLE=%s (portal only)",
            role or "(unset)",
        )
        return False
    # 양성 분기는 정상 상태라 `INFO` 예요. 보이는 이유는
    # `_ensure_agora_log_visibility()` 가 `agora` 로거를 INFO 로 열어 두기 때문이에요 —
    # 부재만으로 판정하면 로그 포맷이 바뀌어도 「사라졌다」로 읽혀요.
    _log.info("shared resource cleanup enabled: AGORA_ROLE=portal")
    return True


# DevIdentityService가 server를 import하지 않도록 composition root에서 owner 판정을 주입해요.
# 서비스의 기본값은 fail-safe하게 actor 없음이고, 실제 API 앱만 이 단일 predicate를 써요.
set_dev_identity_retry_actor_available(_shared_resource_cleanup_owner)


def _register_dev_identity_sweeper(started: list, *, interval: float) -> None:
    """실제 lifespan 목록에 dev identity sweeper task를 등록해요."""
    from .domains.identity.dev_identity_sweeper import (
        run_dev_identity_sweeper,
    )
    from .shared.deps import get_dev_identity_service

    if not _shared_resource_cleanup_owner():
        return
    started.append(asyncio.create_task(run_dev_identity_sweeper(
        get_dev_identity_service(),
        interval=max(interval, 60.0),
        sleep=asyncio.sleep,
        should_run=lambda: True,
    )))


def _shared_policy_reclaimer_enabled(config) -> bool:
    """공유 ① 정책의 옛 리비전 회수 pass 를 켤지 판정해요 (IH-154).

    이 pass 는 **라이브 Cedar 정책을 지워요.** 좌표가 없으면 애초에 컴파일할 수 없으니 조용히
    끄고, 그 외에는 `_shared_resource_cleanup_owner`(= `AGORA_ROLE == portal`) 하나로 판정해요.
    """
    from .shared.deps import shared_gateway_policy_coordinates_configured

    if not shared_gateway_policy_coordinates_configured():
        return False
    # 소유권 판정은 `_shared_resource_cleanup_owner` 한 곳이에요 — 그 docstring 에
    # 「테이블 이름으로 판정하면 안 되는 이유」(라이브 stage=prod / table=dev 실측)가 있어요.
    return _shared_resource_cleanup_owner()


def _configuration_fingerprint() -> dict[str, str | None]:
    from .shared.config import load_config
    config = load_config()
    return {
        "role": config.role,
        "registry_namespace": config.registry_namespace,
        "registry_id": config.registry_id,
        "identity_table": config.identity_table,
    }


@asynccontextmanager
async def _lifespan(app):
    """기동 시 백엔드 모드 로깅 + (설정 시) 배포 job 백그라운드 폴러 기동.

    import 시점이 아니라 실제 기동 시점에만 돌아, 테스트 collection과 분리돼요
    (TestClient를 with 없이 쓰면 lifespan이 안 타므로 테스트 동작 불변).
    """
    from .shared.deps import log_backend_mode
    log_backend_mode()
    _log.info("configuration_fingerprint=%s", _configuration_fingerprint())

    tasks = []
    # 임시 기능(GA 때 제거): 로컬 폴러 ON/OFF 스위치.
    # 폴러 task 생성을 팩토리로 묶어, 부팅 때 켜든 화면에서 나중에 켜든 같은 코드가
    # 돌게 해요. 단일 출처는 shared/dev_poller_switch.py 예요.
    def _start_poller_tasks() -> list:
        started: list = []
        from datetime import datetime, timezone
        from .shared.config import load_config
        from .shared.deps import get_deploy_service, get_request_log, get_gov_store
        from .domains.runtime.deploy.poller import run_poller
        from .domains.governance.poller import run_gov_poller, running_record_ids
        from .domains.governance.scan_service import (
            sync_running_stepfn, apply_verdict_to_registry,
        )
        interval = load_config().poller_interval

        def _now() -> str:
            return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        started.append(asyncio.create_task(run_poller(
            get_deploy_service(), get_request_log(),
            now=_now, interval=interval, sleep=asyncio.sleep,
            should_run=lambda: True,
        )))

        # 거버넌스 스캔 폴러 — SF 완료를 done으로 당기고 verdict를 registry에 반영(멱등).
        def _gov_sync(rid: str) -> None:
            sync_running_stepfn(rid)          # SF SUCCEEDED → done 전이(멱등)
            apply_verdict_to_registry(rid)    # done → APPROVED/REJECTED(멱등)

        started.append(asyncio.create_task(run_gov_poller(
            interval=interval, sleep=asyncio.sleep, should_run=lambda: True,
            sync_fn=_gov_sync,
            running_ids_fn=lambda: running_record_ids(get_gov_store()),
        )))

        # Verdict backstop — stepfn 스캔에서 aggregate Lambda가 "done"을 Dynamo에 직접 push하면
        # gov 폴러가 그 레코드를 running 창에서 놓쳐 apply_verdict_to_registry를 못 불러
        # PENDING에 고착돼요(파이프라인·중복검사는 됐는데 자동승인만 안 됨). done인데 registry가
        # 아직 non-terminal인 레코드를 찾아 verdict를 재적용해요(멱등). run_gov_poller를 재사용하고
        # settled 집합으로 terminal 레코드의 registry 조회를 바운드해요.
        from .domains.catalog.registry.models import RecordNotFound, RecordStatus
        from .domains.governance.poller import (
            done_record_ids, pending_verdict_record_ids,
        )
        _verdict_settled: set[str] = set()
        _terminal_status = {
            RecordStatus.APPROVED, RecordStatus.REJECTED, RecordStatus.DEPRECATED,
        }

        def _registry_status(rid: str):
            from .shared.deps import get_registry, get_registry_id
            try:
                return get_registry().get_record(get_registry_id(), rid).status
            except RecordNotFound:
                return None  # 영구 부재(삭제된 레코드) — settle해 재시도 안 함

        def _pending_verdict_record_ids() -> list[str]:
            store = get_gov_store()
            # running_ids로 재스캔 레코드를 settled에서 풀어 re-arm(M1). done_ids는 재적용 후보.
            return pending_verdict_record_ids(
                done_ids=done_record_ids(store),
                running_ids=running_record_ids(store),
                status_of=_registry_status,
                is_terminal=lambda s: s in _terminal_status,
                settled=_verdict_settled,
            )

        started.append(asyncio.create_task(run_gov_poller(
            interval=interval, sleep=asyncio.sleep, should_run=lambda: True,
            sync_fn=apply_verdict_to_registry,
            running_ids_fn=_pending_verdict_record_ids,
        )))

        # Dev credentials expire independently of request traffic. The sweep removes
        # their Cedar permits as well as marking the credential revoked.
        #
        # ⚠️ 이 pass 는 공유 ① Cedar 정책을 **만들기도** 해요 — 만료 회수의 재프로비저닝
        # (ADR-0108 결정 2)과, 미수렴 발급의 자동 수렴 재시도(IH-160 / ADR-0110)예요. 즉
        # 무인 create 주체가 여기 하나 있고, 아래 `shared_policy_reclaimer`(prune 전용,
        # ADR-0107 결정 4)와 역할이 갈려요. 재시도 예산은
        # `dev_identity_service._CONVERGENCE_MAX_RETRIES` 로 바운드돼 있어요 — 무한 재시도는
        # 실패 리비전을 쌓아 engine 당 정책 상한을 넘겨요.
        config = load_config()
        _register_dev_identity_sweeper(started, interval=interval)

        # IH-78: 코드 다운로드는 Cedar 활성화를 기다리지 않아요. 그래서 PENDING 으로 남은
        # dev identity policy 를 관측해 원장을 맞추는 루프가 필요해요 — 없으면 원장이
        # 영구히 PENDING 이라 거짓말을 해요(ADR-0037 §4).
        #
        # ⚠️ 이 주석은 「비파괴라서 정렬 가드와 무관하게 항상 돈다」고 적혀 있었는데
        # **거짓이었어요** (2026-09-05 정정). `reconcile_pending_policies` 는 superseded
        # 옛 revision 을 실제로 **지워요**(`dev_identity_service.py` 의
        # 「## 더 이상 「비파괴」 가 아니에요」 절). 무조건 도는 근거는 「비파괴」가 아니라
        # **삭제 대상의 좁음**이에요 — 원장이 `policyId` 로 소유를 확인한, 최신보다 낮은
        # per-agent revision 만 지워요. 그 성질이 없는 pass 를 이 주석을 근거로 무조건
        # 등록하지 마세요(아래 공유 정책 회수 pass 는 그래서 별 가드를 받아요).
        from .domains.identity.dev_policy_reconciler import (
            run_dev_policy_reconciler,
        )
        from .shared.deps import get_dev_identity_service

        started.append(asyncio.create_task(run_dev_policy_reconciler(
            get_dev_identity_service(),
            interval=max(interval, 10.0),
            sleep=asyncio.sleep,
            should_run=lambda: True,
        )))

        # IH-154: 활성화 예산을 넘긴 재프로비저닝이 남긴 **공유 ① 정책의 옛 ACTIVE
        # 리비전**을 회수해요. Cedar permit 은 합집합이라 옛 리비전이 남아 있으면 어드민의
        # 회수가 실제로 좁혀지지 않아요. HTTP 경로는 ALB·CloudFront 수명 때문에 이 수렴을
        # 맡을 수 없어서 폴러가 이어받아요.
        #
        # ⚠️ 파괴적이에요(라이브 Cedar 정책 삭제) — `_shared_policy_reclaimer_enabled` 로
        # 정렬 가드를 걸고, 주기는 다른 파괴적 pass 와 같게 최소 60초예요. 회수 pass 는
        # 정책을 **만들지 않아요**(`reclaim_stale_revisions` docstring — 중복 이름은
        # `ConflictException` 이라, 만들기까지 하면 동시 어드민 승인과 반드시 경합해요).
        from .domains.identity.shared_policy_reclaimer import (
            run_shared_policy_reclaimer,
        )
        from .shared.deps import (
            reclaim_stale_shared_gateway_policy_revisions,
        )

        if _shared_policy_reclaimer_enabled(config):
            started.append(asyncio.create_task(run_shared_policy_reclaimer(
                reclaim_stale_shared_gateway_policy_revisions,
                interval=max(interval, 60.0),
                sleep=asyncio.sleep,
                should_run=lambda: True,
            )))
        return started

    # MCP 도구 목록 드리프트 관측 (LC-03). 배포 폴러와 **별도 스위치**예요 —
    # 이쪽은 외부(우리 통제 밖) MCP endpoint 를 주기적으로 두드리므로, 어느 프로세스가
    # 그 트래픽을 내보내는지 명시적으로 고르게 해요. 연결형 MISSING 은 Gateway 동기화
    # 쓰기를 수행하지만 Target 이름이나 정책을 바꾸지 않으므로 배포 정렬 가드와 무관해요
    # (ADR-0089).
    from .shared.config import load_config as _load_config
    _drift_cfg = _load_config()
    if _drift_cfg.mcp_drift_poll_enabled:
        from .domains.catalog.mcp.drift_poller import run_mcp_drift_poller
        from .shared.deps import get_mcp_drift_service

        tasks.append(asyncio.create_task(run_mcp_drift_poller(
            get_mcp_drift_service(),
            interval=_drift_cfg.mcp_drift_poll_interval,
            sleep=asyncio.sleep,
            should_run=lambda: True,
        )))

    from .shared import dev_poller_switch
    _env_owns_poller = _poller_enabled()
    dev_poller_switch.register(
        _start_poller_tasks, already_running=_env_owns_poller)
    if _env_owns_poller:
        tasks.extend(_start_poller_tasks())
    try:
        yield
    finally:
        for t in tasks:
            t.cancel()
        # 임시(GA 때 제거): 화면에서 나중에 켠 폴러도 같이 멈춰요. `tasks` 에는 부팅
        # 시점에 만든 것만 있어서, 이 줄이 없으면 종료 후에도 task 가 남아요.
        dev_poller_switch.disable_for_shutdown()


app = FastAPI(
    title="Agora API", version="0.1.0", description="사내 Agent 마켓플레이스",
    lifespan=_lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(IdentityMiddleware)

# ── 도메인 라우터 ────────────────────────────────────────────────────
app.include_router(dev_poller_router)   # 임시(GA 때 제거)
app.include_router(catalog_router)
app.include_router(mcp_drift_router)
app.include_router(identity_router)
app.include_router(dev_identity_router)
app.include_router(identity_access_router)
app.include_router(identity_internal_router)
app.include_router(identity_inventory_gate_router)
app.include_router(identity_domain_policy_router)
app.include_router(identity_users_router)
app.include_router(identity_directory_router)
app.include_router(governance_router)
app.include_router(bundle_router)
app.include_router(publish_router)
app.include_router(playground_router)
app.include_router(monitoring_router)
# 런타임 배포 라우트(/api/mcp/deploy/*, /api/agent/deploy/*)는 W0 refactor로 catalog에서
# runtime 도메인으로 분리됐어요. 경로·응답 계약은 그대로예요.
app.include_router(runtime_router)


@app.get("/api/health")
def health():
    """헬스 체크. 백엔드는 AWS 단일이에요(AgentCore Registry + S3/DynamoDB)."""
    return {
        "status": "ok",
        "backend": "aws",
        "configuration": _configuration_fingerprint(),
    }
