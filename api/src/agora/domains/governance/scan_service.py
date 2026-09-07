"""스캔 오케스트레이션 — 자동/수동 통일 실행 경로 (§2.5).

트리거만 다르고 실행 로직은 동일. 결과는 GovStore에 ScanRecord로 기록.
P1: 스캔 대상 소스는 source_store에서 로드(없으면 빈 목록으로 스캔 — descriptors 기반).

스토어·스캐너는 생성 시점이 아니라 run() 시점에 deps 접근자로 해석해요.
테스트 conftest가 _isolate_gov_store 픽스처로 deps._gov_store를 테스트마다 교체하는데,
_scan_service 는 리셋하지 않으므로, 생성 시점에 스토어를 캡처하면 이전 테스트의 낡은
스토어를 참조해 스캔 기록이 유실돼요. 그래서 run() 마다 현재 스토어를 다시 읽어요.
"""
from __future__ import annotations

import datetime as _dt
import logging

from .models import ScanRecord

_log = logging.getLogger(__name__)


def _ensure_pending_before_verdict(registry, reg_id, record_id) -> None:
    """DRAFT면 먼저 PENDING_APPROVAL로 승격 — DRAFT→APPROVED/REJECTED는 불법 전이라
    스캔시작 전이가 누락돼도 verdict가 self-heal하게 해요. best-effort."""
    from ...domains.catalog.registry.models import RecordStatus
    try:
        rec = registry.get_record(reg_id, record_id)
        if rec.status is RecordStatus.DRAFT:
            registry.update_status(reg_id, record_id, RecordStatus.PENDING_APPROVAL,
                                   reason="verdict 직전 DRAFT→PENDING 승격")
    except Exception:
        pass


def _inline_source_files(descriptors: dict) -> list:
    """descriptors의 인라인 본문을 [(path, bytes)]로. 없으면 [].

    소스 스토어(source_prefix 4-part)를 안 쓰고 본문을 descriptors에 직접 담는 자산이
    있어요 — /api/catalog/publish 로 올린 skill이 대표적이에요(skill.markdown). 이 본문도
    엄연히 검사 대상 소스라, 소스 기반 도구(gitleaks·semgrep)가 읽을 수 있게 파일로 만들어요.
    AWS 왕복 후엔 agentSkills.skillMd.inlineContent 형태로도 올 수 있어 둘 다 봐요.
    """
    if not isinstance(descriptors, dict):
        return []
    node = descriptors.get("skill")
    if isinstance(node, dict):
        markdown = node.get("markdown")
        if isinstance(markdown, str) and markdown.strip():
            return [("SKILL.md", markdown.encode("utf-8"))]
    aws_node = descriptors.get("agentSkills")
    if isinstance(aws_node, dict):
        skill_md = aws_node.get("skillMd")
        if isinstance(skill_md, dict):
            inline = skill_md.get("inlineContent")
            if isinstance(inline, str) and inline.strip():
                return [("SKILL.md", inline.encode("utf-8"))]
    return []


def _scan_id_for(record_id: str) -> str:
    """SF 실행 이름으로 쓸 유니크 scan_id. record_id + 현재시각(초).

    SF execution name 제약(영숫자·하이픈·최대 80자)에 맞게 slug화해요. 재스캔마다
    달라야 ExecutionAlreadyExists 없이 새 실행이 돌아요(같은 초 내 중복은 이론상
    가능하나 사람이 누르는 재스캔 간격에선 사실상 없음).
    """
    import re
    stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%d%H%M%S")
    raw = f"{record_id}-{stamp}"
    return re.sub(r"[^a-zA-Z0-9]+", "-", raw).strip("-")[:80] or f"scan-{stamp}"


def sync_running_stepfn(record_id: str) -> None:
    """running 스캔이 stepfn이면 SF 완료를 앱 done으로 당겨요(폴링 훅 공용).

    scan_status·gates 등 진행상태를 읽는 모든 경로가 이걸 호출해요 — UI가 어느 쪽을
    폴링하든 SF SUCCEEDED가 done으로 전이돼야 running에 갇히지 않아요(회귀 방지).
    running이 아니거나 스캐너가 sync_execution을 안 가지면 무영향.
    """
    from ...shared.deps import get_gov_store, get_scanner

    store = get_gov_store()
    rec = store.latest_scan(record_id)
    if rec is None or rec.status != "running":
        return
    scanner = get_scanner()
    if not hasattr(scanner, "sync_execution"):
        return
    # running 레코드에 저장된 scan_id로 SF 실행을 조회해요. 없으면(구 레코드 하위호환)
    # record_id를 scan_id로 쓰던 예전 규약으로 폴백.
    scan_id = getattr(rec, "scan_id", "") or record_id
    try:
        scanner.sync_execution(record_id, scan_id, store)
    except Exception:
        return  # sync 실패는 running 유지(다음 폴링 재시도)
    # SF 완료로 done 전이됐으면 verdict를 registry 상태에 반영(자동 approve/reject).
    # done이 아니면 헬퍼 내부에서 early-return하므로 무조건 호출해도 안전.
    apply_verdict_to_registry(record_id)


def apply_verdict_to_registry(record_id: str) -> None:
    """최신 스캔의 gate verdict를 registry 상태로 반영해요.

    auto-approve → APPROVED(+이전 버전 숨김), auto-reject → REJECTED,
    pending/none → 전이 없음(PENDING_APPROVAL 유지, reviewer 수동 판정 대기).
    전이 실패(금지 전이·조회 불가)는 흡수 — verdict 재계산 시 재시도.

    전이가 일어나지 않는 모든 경로는 **사유를 원장에 남기고 로그를 찍어요**(R3).
    예전엔 담당자 미비·전이 실패·pending이 아무 흔적 없이 return해서, 화면엔 "자동
    APPROVE"인데 상태는 "심사중"인 채로 아무도 원인을 알 수 없었어요. 판정 로직은
    그대로예요 — 기록만 추가돼요.
    """
    from ...shared.deps import get_gov_store, get_registry, get_registry_id
    from ...domains.catalog.registry.models import RecordStatus
    from ...shared.trust import ApprovalBlockReason
    from . import approval_block
    from .gate import compute_gates, gate_summary
    from .queue_router import tier_for_record, _asset_key, _has_source
    from .scan_applicability import scan_applicability_for_record
    from .tool_catalog import list_catalog_tools

    store = get_gov_store()
    scan = store.latest_scan(record_id)
    if scan is None or scan.status != "done":
        return  # running/none 이면 아직 verdict 없음
    tier = tier_for_record(record_id)
    applicability = scan_applicability_for_record(record_id)
    stages = compute_gates(tier=tier, cells=store.get_tier(tier),
                           tools=list_catalog_tools(), scan=scan,
                           asset_type=_asset_key(record_id),
                           has_source=_has_source(record_id),
                           scan_applicability=applicability.state)
    verdict = gate_summary(stages)["verdict"]
    target = {"auto-approve": RecordStatus.APPROVED,
              "auto-reject": RecordStatus.REJECTED}.get(verdict)
    if target is None:
        # pending/none — 전이 없음. 화면에 "왜 기다리는지"가 보이도록 사유를 남겨요.
        detail, remediation, waiting = approval_block.describe_gate_wait(stages, verdict)
        _log.info("자동승인 보류: 게이트 판정 미확정 (%s)", verdict,
                  extra={"record_id": record_id, "verdict": verdict})
        approval_block.record(record_id, ApprovalBlockReason.GATE_PENDING,
                              detail=detail, remediation=remediation,
                              incomplete_stages=waiting, verdict=verdict, store=store)
        return

    registry, reg_id = get_registry(), get_registry_id()
    # Review submission is allowed without contacts. The responsibility contract
    # blocks only the final production approval.
    _ensure_pending_before_verdict(registry, reg_id, record_id)
    if target is RecordStatus.APPROVED:
        from ...shared.deps import get_asset_responsibility_port
        from ...shared.responsibility import ResponsibilityIncomplete

        try:
            get_asset_responsibility_port().require_for_approval(record_id)
        except ResponsibilityIncomplete as exc:
            # 담당자 필수 규칙은 의도된 계약이라 그대로 막아요. 다만 **무엇이 비었는지**를
            # 남겨야 등록자가 스스로 풀 수 있어요(예전엔 로그조차 없었어요).
            missing = list(exc.status.blocking_reasons)
            _log.warning(
                "자동승인 보류: 담당자 연락 계약 미완 (%s)", ", ".join(missing) or "이유 미상",
                extra={"record_id": record_id, "blocking_reasons": missing},
            )
            approval_block.record(
                record_id, ApprovalBlockReason.RESPONSIBILITY_INCOMPLETE,
                detail="production 승인에 필요한 담당자 정보가 아직 없어요 — "
                       + ", ".join(approval_block.contact_labels(missing)) + ".",
                remediation=approval_block.RESPONSIBILITY_REMEDIATION,
                missing_contacts=missing, verdict=verdict, store=store,
            )
            return
        except Exception as exc:
            # 관측 실패는 미비와 다른 사실이에요 — 합치면 엉뚱한 곳을 고치게 돼요(ADR-0037 §4).
            _log.warning(
                "asset responsibility could not be observed; approval remains pending",
                extra={"record_id": record_id},
                exc_info=True,
            )
            approval_block.record(
                record_id, ApprovalBlockReason.RESPONSIBILITY_UNOBSERVABLE,
                detail=f"담당자 정보를 조회하지 못했어요({type(exc).__name__}) — "
                       "비어 있다는 뜻이 아니라 확인 자체를 못 했어요.",
                remediation="일시 오류일 수 있어요. 잠시 후 재스캔하고, 반복되면 관리자에게 알려주세요.",
                verdict=verdict, store=store,
            )
            return
    if target is RecordStatus.REJECTED:
        # 자동 반려도 "자동승인이 안 된 이유"예요 — 등록자가 무엇 때문에 막혔는지 봐야 해요.
        detail, failed = approval_block.describe_gate_reject(stages)
        _log.info("자동 반려: 필수 게이트 미통과", extra={"record_id": record_id})
        approval_block.record(record_id, ApprovalBlockReason.GATE_REJECTED,
                              detail=detail,
                              remediation="위협리포트의 지적사항을 고쳐 새 버전을 올린 뒤 재스캔하세요.",
                              incomplete_stages=failed, verdict=verdict, store=store)
    try:
        registry.update_status(reg_id, record_id, target,
                               reason=f"governance verdict: {verdict}")
    except Exception as exc:
        # 전이 실패는 다음 폴링에서 재시도해요. 그동안 상태는 "심사중"인데 판정은 났으니
        # 사유를 남기지 않으면 관리자가 원인을 알 방법이 없어요(조용한 경로 ②).
        _log.warning("자동 %s 전이 실패 — 다음 폴링에서 재시도해요", target.value,
                     extra={"record_id": record_id}, exc_info=True)
        approval_block.record(
            record_id, ApprovalBlockReason.STATUS_TRANSITION_FAILED,
            detail=f"게이트 판정({verdict})은 났지만 상태를 {target.value}로 바꾸지 못했어요"
                   f"({type(exc).__name__}: {exc}).",
            remediation="다음 폴링에서 자동 재시도해요. 계속되면 승인 큐에서 직접 판정하세요.",
            verdict=verdict, store=store,
        )
        return

    # APPROVED 전이 성공 시에만 같은 소스 자산의 이전 버전을 숨겨요(catalog 공백 방지).
    if target is RecordStatus.APPROVED:
        # 승인됐으니 차단 사유는 더 이상 사실이 아니에요 — 지워요(오래된 사유 표시 방지).
        approval_block.clear(record_id, store=store)
        _hide_sibling_versions(record_id)


def _hide_sibling_versions(record_id: str) -> None:
    """record_id가 APPROVED됐을 때 같은 소스 자산의 다른 버전을 숨겨요(catalog 공백 방지).

    auto(sync)·수동(_decide) 승인 양쪽에서 재사용해요. best-effort — 조회/전이 실패는 흡수.
    """
    from ...shared.deps import get_registry, get_registry_id

    registry, reg_id = get_registry(), get_registry_id()
    try:
        approved = registry.get_record(reg_id, record_id)
        asset_id = _source_asset_id_of(approved)
        if not asset_id:
            return
        for sib in registry.find_by_source_asset_id(reg_id, asset_id):
            if sib.record_id != record_id and sib.search_visible:
                try:
                    registry.set_search_visible(reg_id, sib.record_id, False)
                except Exception:
                    pass
    except Exception:
        pass


def _source_asset_id_of(record) -> str:
    """RegistryRecord의 source_prefix(`{type}/{owner}/{name}/{version}/`)에서 `owner/name` 추출."""
    parts = [p for p in (record.source_prefix or "").split("/") if p]
    return f"{parts[1]}/{parts[2]}" if len(parts) >= 3 else ""


class ScanService:
    def __init__(self, scanner=None, store=None, source_store=None, registry=None, registry_id=None):
        self._scanner = scanner
        self._store = store
        self._source_store = source_store
        self._registry = registry
        self._registry_id = registry_id

    def _resolve_store(self):
        if self._store is not None:
            return self._store
        from ...shared.deps import get_gov_store
        return get_gov_store()

    def _resolve_scanner(self):
        if self._scanner is not None:
            return self._scanner
        from ...shared.deps import get_scanner
        return get_scanner()

    def _transition_registry(self, record_id: str, target) -> None:
        """registry 상태를 best-effort로 전이해요(스캔/판정을 막지 않음).

        registry 미주입·조회 실패·금지 전이(예: 이미 APPROVED)는 조용히 흡수해요.
        """
        if self._registry is None or not self._registry_id:
            return
        try:
            self._registry.update_status(self._registry_id, record_id, target)
        except Exception:
            pass

    def run_async(self, record_id: str, trigger: str = "manual-queue", principal: str = "",
                  executor=None) -> ScanRecord:
        """스캔을 비동기로 시작해요 — 'running' 레코드를 즉시 남기고 즉시 반환.

        실제 스캔(run)은 executor로 백그라운드 실행돼 완료 시 'done' 레코드로 최종 갱신돼요.
        큐/상세는 latest_scan.status가 'running'이면 진행중으로 표시하고 재스캔 버튼을 disabled 처리해요.
        executor 미지정 시 데몬 스레드로 실행(요청은 즉시 응답). 테스트는 동기 executor를 주입해요.
        이미 running이면 새 스캔을 시작하지 않고 기존 running을 반환해요(중복 실행 방지 — 버튼
        여러 번 눌러도 스캔은 한 번만).
        """
        from .scan_applicability import require_scan_applicable

        require_scan_applicable(
            record_id,
            registry=self._registry,
            registry_id=self._registry_id,
            source_store=self._source_store,
        )
        store = self._resolve_store()
        existing = store.latest_scan(record_id)
        if existing is not None and existing.status == "running":
            return existing

        running = ScanRecord(
            ts=_dt.datetime.now(_dt.timezone.utc).isoformat(),
            risk="none", trigger=trigger, principal=principal, status="running",
        )
        self._resolve_store().add_scan(record_id, running)

        # 스캔 시작 = 심사 착수 → registry를 PENDING_APPROVAL로. auto/수동 스캔 공통 경로.
        # RecordStatus는 catalog.registry.models 것이라야 update_status 인자와 일치해요.
        from ...domains.catalog.registry.models import RecordStatus  # 지역 import: 순환 방지
        self._transition_registry(record_id, RecordStatus.PENDING_APPROVAL)

        def _work():
            # 스캔 자체 실패로 프로세스가 죽지 않게 감싸요 — 실패해도 done(fail-closed)으로 기록.
            try:
                self.run(record_id, trigger=trigger, principal=principal)
            except Exception as e:  # pragma: no cover - 방어적
                self._resolve_store().add_scan(record_id, ScanRecord(
                    ts=_dt.datetime.now(_dt.timezone.utc).isoformat(),
                    risk="high", trigger=trigger, principal=principal, status="done",
                    findings=[{"code": "SCANNER_ERROR", "severity": "high",
                               "detail": f"스캔 실행 오류: {type(e).__name__}", "location": ""}],
                ))

        if executor is None:
            import threading
            threading.Thread(target=_work, daemon=True).start()
        else:
            executor(_work)
        return running

    def rescan_tool(self, record_id: str, tool_id: str, trigger: str = "manual-detail",
                    principal: str = "", executor=None) -> ScanRecord:
        """단일 도구(게이트 단계)만 재실행해요 — 부분 재스캔.

        base done scan에서 이 도구의 area만 갈아끼우는 게 목적이에요(전체 결과 소실 방지).
        running placeholder에 base(findings/scanned_areas)와 rescan_area를 실어 두고, SF 완료
        시 sync_execution이 merge_partial_scan으로 합쳐 done을 굳혀요. static 등 동기 스캐너는
        여기서 즉시 merge해 done을 저장해요.

        부분 재스캔이 부적합한 상황은 ValueError로 거부해요(라우터가 422):
          - base 없음 / base.status != "done": 교체할 base가 없어요 → 전체 스캔 먼저.
          - base에 scan-level 에러(SOURCE_MISSING/SCANNER_ERROR): base가 신뢰 불가 → 전체 재스캔.
          - 이 도구가 게이트에서 not_applicable/off/미존재: 재시도 무의미.
        """
        from .gate import compute_gates
        from .queue_router import tier_for_record
        from .tool_catalog import list_catalog_tools

        store = self._resolve_store()
        base = store.latest_scan(record_id)
        if base is None or base.status != "done":
            raise ValueError("완료된 스캔이 없어요. 먼저 전체 스캔을 실행하세요.")
        # base가 scan-level 에러(소스 로드 실패·스캐너 예외)면 어떤 area도 신뢰할 수 없어요.
        for f in base.findings or []:
            if str(f.get("code", "")) in ("SOURCE_MISSING", "SCANNER_ERROR"):
                raise ValueError("스캔 자체가 실패한 상태예요. 부분 재시도 대신 전체 재스캔을 실행하세요.")

        tier = tier_for_record(record_id)
        from .queue_router import _asset_key, _has_source
        from .scan_applicability import scan_applicability_for_record
        asset_type = _asset_key(record_id)
        has_source = _has_source(record_id)
        applicability = scan_applicability_for_record(
            record_id,
            registry=self._registry,
            registry_id=self._registry_id,
            source_store=self._source_store,
        )
        cells = store.get_tier(tier)
        tools = list_catalog_tools()
        # 게이트 표시와 1:1로 판정 — 이 도구가 not_applicable/off/미존재면 재시도 부적합.
        stages = compute_gates(tier=tier, cells=cells, tools=tools, scan=base,
                               asset_type=asset_type, has_source=has_source,
                               scan_applicability=applicability.state)
        stage = next((s for s in stages if s.tool_id == tool_id), None)
        if stage is None:
            raise ValueError(f"이 자산의 게이트에 없는 도구예요: {tool_id}")
        if stage.state == "not_applicable":
            raise ValueError("이 자산엔 해당 도구의 검사 대상이 없어요(해당 없음).")

        tool = next((t for t in tools if t.tool_id == tool_id), None)
        area = tool.area if tool is not None else stage.area
        # 이 도구의 tier 셀 1개만 남겨 descriptors.cells에 실어요(스캐너가 이 도구만 실행).
        cell = next((c for c in cells if c.tool_id == tool_id), None)
        if cell is None:
            raise ValueError(f"이 도구의 등급 셀이 없어요: {tool_id}")

        # running placeholder에 base(findings/scanned_areas/scanner_kind)와 rescan_area를 실어요.
        # merge 시점(sync_execution)에 base를 읽어 이 area만 교체하게 하려는 거예요. 같은 scan_id로
        # 저장돼 SF 완료 done이 이 running을 같은 SK로 덮어써요.
        scan_id = _scan_id_for(record_id)
        version = self._record_version(record_id)
        running = ScanRecord(
            ts=_dt.datetime.now(_dt.timezone.utc).isoformat(),
            risk=base.risk, findings=list(base.findings), trigger=trigger,
            principal=principal, status="running",
            scanned_areas=list(base.scanned_areas), scanner_kind=base.scanner_kind,
            scan_id=scan_id, version=version, rescan_area=area,
        )
        store.add_scan(record_id, running)

        # 재스캔 시작 = 재심사 착수 → registry를 PENDING_APPROVAL로(run_async와 동일). APPROVED
        # 자산을 재시도해도 registry가 APPROVED에 머무르지 않게 해요. best-effort·멱등
        # (DRAFT/PENDING/APPROVED 모두 안전, 금지 전이는 _transition_registry가 흡수).
        from ...domains.catalog.registry.models import RecordStatus  # 지역 import: 순환 방지
        self._transition_registry(record_id, RecordStatus.PENDING_APPROVAL)

        # descriptors는 run()과 같은 방식이되 cells를 이 도구 1개로 좁혀요. 소스 로드도 동일.
        asset_type_src, source_files, status = self._load_source(record_id)
        descriptors = {
            "scan_id": scan_id, "record_id": record_id,
            "has_source": status == "loaded",
            "cells": [{"tier": cell.tier, "tool_id": cell.tool_id, "enforcement": cell.enforcement}],
        }
        try:
            settings = store.get_settings()
            descriptors["judge_model"] = settings.judge_model_map.get(tier, "haiku-4-5")
        except Exception:
            pass
        if status != "loaded":
            descriptors["judge_document"] = self._judge_document(record_id, asset_type_src)

        def _work():
            outcome = self._resolve_scanner().scan(asset_type_src, descriptors, source_files)
            if getattr(outcome, "pending", False):
                # 비동기(stepfn): running 유지. SF 완료 폴링(sync_execution)이 merge해 done 전이.
                return
            # 동기(static 등): 즉시 base와 merge해 done을 저장해요(부분 결과가 전체를 덮지 않게).
            from .scan_orchestration import merge_partial_scan
            merged = merge_partial_scan(
                list(base.findings), list(base.scanned_areas), area,
                list(getattr(outcome, "findings", None) or []),
                list(getattr(outcome, "scanned_areas", None) or []))
            done = ScanRecord(
                ts=_dt.datetime.now(_dt.timezone.utc).isoformat(),
                risk=merged["risk"], findings=merged["findings"], trigger=trigger,
                principal=principal, status="done", scanned_areas=merged["scanned_areas"],
                scanner_kind=getattr(outcome, "scanner_kind", "") or base.scanner_kind,
                scan_id=scan_id, version=version,  # rescan_area는 done엔 실지 않음("").
            )
            store.add_scan(record_id, done)
            apply_verdict_to_registry(record_id)

        if executor is None:
            import threading
            threading.Thread(target=_work, daemon=True).start()
        else:
            executor(_work)
        return running

    def run(self, record_id: str, trigger: str = "manual-queue", principal: str = "") -> ScanRecord:
        from .scan_applicability import require_scan_applicable

        require_scan_applicable(
            record_id,
            registry=self._registry,
            registry_id=self._registry_id,
            source_store=self._source_store,
        )
        # 스캔 시작 = 심사 착수 → registry를 PENDING_APPROVAL로. auto_scan은 run_async가 아니라
        # run()을 직접 부르므로(§auto), 여기서도 전이해야 auto-scanned 자산이 DRAFT에 안 갇혀요.
        # best-effort·idempotent: DRAFT→PENDING만 합법, 이미 PENDING/APPROVED면 _transition_registry가 흡수.
        from ...domains.catalog.registry.models import RecordStatus  # 지역 import: 순환 방지
        self._transition_registry(record_id, RecordStatus.PENDING_APPROVAL)

        # 어느 버전을 검사했는지 남겨요 — 재배포 후 재스캔 판단에 써요.
        version = self._record_version(record_id)

        # L1: 자산 소스를 로드해 스캐너에 전달. 소스 상태에 따라 분기.
        asset_type, source_files, status = self._load_source(record_id)

        if status == "error":
            # 소스 관리형인데 로드 실패 → 스캐너를 돌리면 빈 검사가 "통과"로 오인돼요.
            # fail-closed: SOURCE_MISSING finding + risk=high, 실제 검사한 area 없음(scanned_areas 빔).
            rec = ScanRecord(
                ts=_dt.datetime.now(_dt.timezone.utc).isoformat(),
                risk="high", trigger=trigger, principal=principal, status="done",
                findings=[{
                    "code": "SOURCE_MISSING", "severity": "high", "location": "",
                    "detail": "자산 소스를 로드하지 못했어요(미러/소스 스토어에 없음). 재업로드 후 재스캔하세요.",
                }],
                scanned_areas=[],
                version=version,
            )
            self._resolve_store().add_scan(record_id, rec)
            # done 스캔 → verdict를 registry에 반영(static/동기 경로엔 stepfn 폴링 훅이 없어서
            # 여기서 안 부르면 PENDING에 고립돼요). SOURCE_MISSING은 gate가 not_run→verdict
            # "pending"이라 전이 없이 PENDING 유지(fail-closed). apply_verdict는 done-guard+멱등.
            apply_verdict_to_registry(record_id)
            return rec

        # stepfn 스캐너가 자산 tier로 도구목록을 구성하도록 descriptors에 cells/scan_id를 실어요.
        # (static/fargate 스캐너는 descriptors를 무시 — 하위호환.)
        # scan_id는 재스캔마다 유니크해야 해요 — record_id로 고정하면 SF가 같은 실행 이름을
        # ExecutionAlreadyExists로 거부해 재스캔이 옛 결과를 재사용해요(실 e2e 결함). record_id +
        # 현재시각(초)으로 유니크화하고, running 레코드에 저장해 폴링이 이 값으로 SF를 조회하게 해요.
        scan_id = _scan_id_for(record_id)
        # record_id는 stepfn 스캐너가 SF payload에 실어 aggregate Lambda가 DynamoGovStore에
        # 결과를 직접 write할 때 써요(push 경로). static/fargate 스캐너는 무시 — 하위호환.
        # has_source: 소스 번들 유무. False면 스캐너가 소스 기반 도구를 실행 목록에서 빼고
        # (게이트 not_applicable과 1:1), judge_document를 llm-judge 입력으로 올려요.
        has_source = status == "loaded"
        descriptors = {"scan_id": scan_id, "record_id": record_id, "has_source": has_source}
        try:
            from .queue_router import tier_for_record
            tier = tier_for_record(record_id)
            descriptors["cells"] = [
                {"tier": c.tier, "tool_id": c.tool_id, "enforcement": c.enforcement}
                for c in self._resolve_store().get_tier(tier)
            ]
            settings = self._resolve_store().get_settings()
            descriptors["judge_model"] = settings.judge_model_map.get(tier, "haiku-4-5")
        except Exception:
            pass  # tier 해석 실패 시 스캐너 자체 폴백에 맡김
        if not has_source:
            descriptors["judge_document"] = self._judge_document(record_id, asset_type)
        outcome = self._resolve_scanner().scan(asset_type, descriptors, source_files)
        if getattr(outcome, "pending", False):
            # 비동기 스캐너(stepfn)가 실행만 시작하고 결과는 미확정 — done으로 굳히면 빈 검사가
            # "통과"로 오인돼요(fake auto-approve). running으로 남겨 게이트가 pending 처리하게 해요.
            # 실제 done 전이는 SF 완료 콜백(P2)이 담당.
            rec = ScanRecord(
                ts=_dt.datetime.now(_dt.timezone.utc).isoformat(),
                risk="none", trigger=trigger, principal=principal, status="running",
                scan_id=scan_id,  # 폴링(sync_running_stepfn)이 이 값으로 SF 실행을 조회해요.
                version=version,
            )
            self._resolve_store().add_scan(record_id, rec)
            return rec
        rec = ScanRecord(
            ts=_dt.datetime.now(_dt.timezone.utc).isoformat(),
            risk=outcome.risk, findings=outcome.findings,
            trigger=trigger, principal=principal, status="done",
            scanned_areas=list(getattr(outcome, "scanned_areas", None) or []),
            # 스캐너 종류를 ScanRecord로 옮겨 gate가 strict 판정하게 해요(StaticScanner="static").
            # 미설정 스캐너(NoopScanner 등)는 ""라 레거시 pass 하위호환 유지.
            scanner_kind=getattr(outcome, "scanner_kind", ""),
            version=version,
        )
        self._resolve_store().add_scan(record_id, rec)
        # 정상 done 스캔 → verdict를 registry에 반영(auto-approve→APPROVED, auto-reject→REJECTED,
        # pending→PENDING 유지). static/fargate/stepfn 모든 경로에서 일관되게 전이하도록 여기서 호출해요.
        # (sync_running_stepfn은 stepfn 폴링 전용 훅이라 static 동기 경로를 안 타서 예전엔 PENDING
        # 고립됐어요. 실 AWS E2E 버그#2 근본수정.) apply_verdict는 done-guard+멱등이라 이중호출 안전.
        # 주의: 위 pending(running) 분기는 verdict 미확정이라 여기 도달 전 return — 절대 안 불러요.
        apply_verdict_to_registry(record_id)
        return rec

    def _record_version(self, record_id: str) -> str:
        """스캔 대상 자산의 버전. 못 알아내면 "".

        ScanRecord.version에 남겨서, 재배포로 코드가 바뀌면 auto_scan이 "스캔 기록이
        있다"는 이유로 건너뛰지 않게 해요(실측 2026-07-27: 새 코드가 무검사 통과).
        """
        if self._registry is None:
            return ""
        try:
            return getattr(self._registry.get_record(self._registry_id, record_id),
                           "version", "") or ""
        except Exception:
            return ""

    def _asset_type_of(self, record_id: str) -> str | None:
        """레코드의 자산 타입 키(mcp/skill/agent). registry descriptor_type이 정본.

        queue_router._asset_key와 같은 매핑(gate.asset_key_of)을 써요 — 두 경로가 타입을
        다르게 구하면 게이트 표시(descriptor_type 기준)와 실행 목록(여기)이 어긋나요.
        실측 2026-07-28: source_prefix에서 파생하던 옛 구현이 connect형 MCP를 "skill"로
        오판해 trivy가 실행 목록에서 조용히 빠졌어요. 조회 실패/미매핑 타입은 None →
        resolve_tools·compute_gates가 자산타입 필터를 적용하지 않아요(전체 노출).
        """
        from .gate import asset_key_of
        if self._registry is None:
            return None
        try:
            rec = self._registry.get_record(self._registry_id, record_id)
        except Exception:
            return None
        dt = getattr(rec, "descriptor_type", None)
        return asset_key_of(dt.value if hasattr(dt, "value") else (str(dt) if dt else None))

    def _judge_document(self, record_id: str, asset_type: str | None) -> str:
        """소스 없는 자산의 llm-judge 입력 문서. registry descriptors에서 생성(실패 시 "")."""
        from .judge_document import build_judge_document
        if self._registry is None:
            return ""
        try:
            rec = self._registry.get_record(self._registry_id, record_id)
        except Exception:
            return ""
        return build_judge_document(getattr(rec, "descriptors", {}) or {},
                                    name=getattr(rec, "name", "") or record_id,
                                    asset_type=asset_type or "")

    def _load_source(self, record_id: str) -> tuple[str | None, list, str]:
        """레코드의 소스를 (asset_type, [(path, bytes)...], status)로 로드해요.

        asset_type은 descriptor_type에서 구해요(_asset_type_of) — source_prefix는 소스
        경로 파싱에만 써요.

        status 3종으로 "소스 관리형인데 로드 실패"와 "소스 비관리"를 구분해요:
          - "loaded" : source_prefix 유효 + 소스 파일을 실제로 읽음.
          - "none"   : source_prefix 없음(소스 비관리 descriptor형) 또는 registry 조회 불가 →
                       소스 기반 도구는 아예 실행 목록에서 빼고(not_applicable) 진행.
          - "error"  : source_prefix는 유효한데(소스 관리형) 소스 스토어에서 못 읽음 →
                       호출부가 fail-closed 처리(빈 검사를 '통과'로 오인 방지, root cause fix).
        """
        registry = self._registry
        asset_type = self._asset_type_of(record_id)
        if registry is None:
            return asset_type, [], "none"
        try:
            rec = registry.get_record(self._registry_id, record_id)
        except Exception:
            # registry 조회 실패 → 소스 관리형 여부 판단 불가. 빈 검사(게시 안 막음).
            return asset_type, [], "none"
        prefix = getattr(rec, "source_prefix", "") or ""
        parts = [p for p in prefix.split("/") if p]
        # 기대: [type, owner, name, version]. 부족하면 소스 스토어 관리형이 아니에요.
        if len(parts) < 4:
            # 인라인 본문(skill markdown)이 descriptors에 있으면 그게 검사 대상 소스예요.
            # 이걸 빼먹으면 인라인 퍼블리시 skill이 "파일 0개"로 스캔돼 gitleaks·semgrep이
            # 아무것도 안 읽고 통과해요(실측 2026-07-28: 인라인 skill 무검사 통과).
            # 소스 스토어를 안 타므로 source_store 주입 여부와 무관해요.
            inline = _inline_source_files(getattr(rec, "descriptors", {}) or {})
            if inline:
                return asset_type, inline, "loaded"
            return asset_type, [], "none"
        # 여기부턴 소스 스토어 관리형 — 스토어가 없으면 로드 판단 불가(기존 동작 유지).
        source_store = self._source_store
        if source_store is None:
            return asset_type, [], "none"
        asset_id = f"{parts[1]}/{parts[2]}"
        version = parts[3]
        try:
            manifest = source_store.get_manifest(asset_id, version)
        except Exception:
            # source_prefix는 있는데 소스를 못 읽음 = 로드 실패. fail-closed 대상.
            return asset_type, [], "error"
        files: list = []
        for path in manifest.paths():
            try:
                files.append((path, source_store.read_file(asset_id, version, path)))
            except Exception:
                continue  # 개별 파일 실패는 건너뛰고 나머지 스캔
        # manifest는 읽혔는데 파일을 하나도 못 읽었으면 로드 실패로 간주.
        if not files:
            return asset_type, [], "error"
        return asset_type, files, "loaded"
