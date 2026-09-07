"""등록 후 중복검토 훅 — 설정이 켜져 있을 때만 유사 자산 후보를 탐색해요.

`auto_scan.maybe_auto_scan`과 같은 자리·같은 계약이에요: 등록 경로가 record 생성 후 호출하고,
기본 OFF이고, 비차단(실패가 등록을 막지 않음)이고, 멱등해요.

**왜 search가 아니라 list_records인가.** `RegistryPort.search`는 APPROVED만 노출하고
(`port.py:91` 규약) AWS 데이터플레인 상한이 20건이에요. 방금 등록된 자산은 DRAFT/
PENDING_APPROVAL이라 자기 자신도, 같이 심사 중인 다른 자산도 search로는 안 보여요.
"등록 시점에 이미 비슷한 게 심사 중"인 경우가 바로 잡아야 할 중복이라서 전수 조회를 써요.

**왜 별도 토글인가.** 스캔은 Fargate/StepFn 비용이 들어 기본 OFF가 합리적이지만, 중복검토는
Registry 읽기 + 순수 계산이라 성격이 달라요. `auto_scan`에 얹으면 "비용 때문에 스캔을 끄면
중복검토도 같이 꺼지는" 원하지 않는 결속이 생겨요.
"""
from __future__ import annotations

import datetime as _dt
import logging
from dataclasses import asdict

from ...shared.deps import (
    get_gov_store,
    get_registry,
    get_registry_id,
    get_source_store,
)
from .models import OverlapCandidate, OverlapRecord
from .overlap import band_of, score_pair, shared_endpoints

_log = logging.getLogger(__name__)

# 저장할 후보 상한. 전수 비교는 하되 결과는 상위 N건만 남겨요 — 검토자가 읽을 수 있는 양이고,
# low band 수십 건이 레코드를 채우면 정작 high 후보가 묻혀요.
_MAX_CANDIDATES = 10

# 이 점수 미만은 후보로 남기지 않아요. 토큰 하나 겹친 정도를 "중복 후보"로 올리면
# 검토 큐가 잡음으로 가득 차서 아무도 안 봐요.
_MIN_SCORE = 20

# 소스 본문을 읽을 최대 후보 수. Skill 자산은 descriptor에 능력 정보가 없어서 본문을 읽어야
# 비교가 되는데, 후보마다 S3를 읽으면 자산 수에 비례해 느려져요(N+1). 이름·설명 축으로 먼저
# 정렬해 상위 후보만 본문을 확인해요 — 완전 복제는 이름·설명도 비슷해서 상위에 올라와요.
_MAX_CONTENT_PROBES = 15

# 소스 본문 비교에 쓸 파일 (asset_type → 파일명). descriptor에 능력 정보가 없는 타입만 대상이에요.
_CONTENT_FILE = {"skill": "SKILL.md"}


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def maybe_overlap_review(
    record_id: str, *, for_version: str = "", trigger: str = "auto",
    principal: str = "system",
) -> bool:
    """중복검토를 (조건이 맞으면) 실행해요. 실행했으면 True.

    **버전 업 배포는 검토하지 않아요.** 두 갈래를 모두 막아요:
      - 재배포(agent·MCP)는 같은 레코드를 갱신하니 기존 검토 기록이 남아 `_should_review`가 막아요.
      - source publish 버전 업(skill)은 **새 레코드**를 만들어서 기록이 없어요. 같은 논리 자산
        (`_lineage_of`)의 다른 버전이 이미 검토됐는지 봐서 막아요.

    `for_version`은 계약 호환으로 받되 판단에 쓰지 않아요(`_should_review` 참조).
    """
    store = get_gov_store()
    if not store.get_settings().auto_overlap_review:
        return False
    if not _should_review(store.latest_overlap(record_id), for_version):
        return False
    if _sibling_version_reviewed(store, record_id):
        return False
    try:
        record = run_overlap_review(record_id, trigger=trigger, principal=principal)
    except Exception:
        # 등록·배포를 중복검토 실패로 되돌리지 않아요. 다음 등록/재배포가 재시도해요.
        _log.warning("중복검토 실행에 실패했어요 — 등록은 계속 진행해요.",
                     extra={"record_id": record_id}, exc_info=True)
        return False
    return record is not None


def run_overlap_review(
    record_id: str, *, trigger: str = "manual", principal: str = "system",
) -> OverlapRecord | None:
    """중복검토를 즉시 실행하고 결과를 저장해요. 대상 레코드를 못 읽으면 None.

    실패도 `status="failed"` 레코드로 남겨요. 레코드가 아예 없으면 "미검토"와 구분되지 않아서,
    실패한 검토가 "중복 없음"으로 읽히거든요.
    """
    store, registry, registry_id = get_gov_store(), get_registry(), get_registry_id()
    try:
        subject = registry.get_record(registry_id, record_id)
    except Exception:
        # 실패도 durable 레코드로 남겨요. 아무것도 남기지 않으면 UI·검토 큐가 "실행했지만
        # 실패"를 "아직 검토 안 함"과 똑같이 보여줘서, 운영자가 재시도가 필요한지 알 수 없어요.
        _log.warning("중복검토 대상 레코드를 읽지 못했어요.",
                     extra={"record_id": record_id}, exc_info=True)
        record = OverlapRecord(ts=_now(), status="failed", trigger=trigger,
                              principal=principal,
                              error="대상 자산을 조회하지 못했어요.")
        try:
            store.add_overlap(record_id, record)
        except Exception:
            # 저장까지 실패하면 남길 방법이 없어요. 다음 순회가 재시도해요.
            return None
        return record

    version = getattr(subject, "version", "") or ""
    try:
        others = registry.list_records(registry_id)
    except Exception:
        record = OverlapRecord(ts=_now(), status="failed", trigger=trigger,
                               principal=principal, version=version,
                               error="자산 목록을 조회하지 못했어요.")
        store.add_overlap(record_id, record)
        return record

    candidates = _rank(subject, others)
    compared = sum(1 for other in others
                   if getattr(other, "record_id", "") != record_id)
    record = OverlapRecord(
        ts=_now(), status="done", trigger=trigger, principal=principal,
        candidates=[asdict(c) for c in candidates],
        version=version, compared=compared,
    )
    store.add_overlap(record_id, record)
    return record


def _rank(subject, others) -> list[OverlapCandidate]:
    """대상 자산과 모든 후보를 비교해 상위 후보를 점수 내림차순으로 돌려줘요."""
    subject_id = getattr(subject, "record_id", "")
    subject_name = getattr(subject, "name", "") or ""
    subject_desc = getattr(subject, "description", "") or ""
    subject_desc_map = getattr(subject, "descriptors", {}) or {}

    # Agora Gateway처럼 여러 자산이 공유하는 endpoint는 식별력이 없어요. 모집단에서 실제
    # 공유 사실을 세서 제외해요(URL 패턴 하드코딩은 환경마다 깨져요).
    #
    # `others`에는 대상 자신이 이미 포함돼요(전수 조회). subject를 한 번 더 붙이면 owner 수가
    # 1 부풀어, **정확히 두 자산만** 같은 upstream을 가리키는 진짜 중복이 `min_owners=3`에
    # 걸려 endpoint 신호를 잃어요. 그래서 모집단은 `others`만으로 세요.
    #
    # 단 `list_records`의 `max_results` 상한에 걸려 대상이 목록에서 빠질 수 있어요. 그때만
    # 보태 넣어 이중 계수 없이 모집단을 정확히 유지해요.
    population = [getattr(r, "descriptors", {}) or {} for r in others]
    if not any(getattr(r, "record_id", "") == subject_id for r in others):
        population.append(subject_desc_map)
    ignored = shared_endpoints(population)

    subject_lineage = _lineage_of(subject)

    # 비교 대상 선별 — 자기 자신과 같은 논리 자산의 다른 버전은 제외해요.
    comparable = []
    for other in others:
        other_id = getattr(other, "record_id", "")
        # 자기 자신 제외. 전수 조회라 대상 자산도 목록에 들어 있어요(빼지 않으면 항상 100점).
        if not other_id or other_id == subject_id:
            continue
        # **같은 논리 자산의 다른 버전 제외.** source publish는 버전마다 새 record를 만들어서,
        # 이름·설명·tool을 그대로 둔 정상 patch 업그레이드가 자기 이전 버전과 high 후보로
        # 잡혀요. 그러면 모든 버전 올림이 검토 큐를 오염시켜요.
        if subject_lineage and _lineage_of(other) == subject_lineage:
            continue
        comparable.append(other)

    # **2단계 랭킹.** 대상이 소스 본문 비교 대상(Skill 등)이면, 후보마다 S3를 읽으면 자산 수에
    # 비례해 느려져요(N+1). 그래서 1차로 본문 없이 점수를 내 정렬하고, 상위 후보만 본문을
    # 읽어 재채점해요. 완전 복제는 이름·설명도 비슷해서 상위에 올라오니 놓치지 않아요.
    subject_content = _source_content(subject)

    def _score(other, other_content: str = ""):
        return score_pair(
            name=subject_name, description=subject_desc, descriptors=subject_desc_map,
            other_name=getattr(other, "name", "") or "",
            other_description=getattr(other, "description", "") or "",
            other_descriptors=getattr(other, "descriptors", {}) or {},
            ignore_endpoints=ignored,
            content=subject_content, other_content=other_content,
        )

    prelim = sorted(
        ((_score(other)[0], getattr(other, "record_id", ""), other)
         for other in comparable),
        key=lambda t: (-t[0], t[1]),
    )
    probe_ids = set()
    if subject_content:
        probe_ids = {rid for _, rid, _ in prelim[:_MAX_CONTENT_PROBES]}

    scored: list[OverlapCandidate] = []
    for _, other_id, other in prelim:
        other_content = ""
        if other_id in probe_ids:
            other_content = _source_content(other)
        score, reasons = _score(other, other_content)
        if score < _MIN_SCORE:
            continue
        scored.append(OverlapCandidate(
            record_id=other_id,
            name=getattr(other, "name", "") or other_id,
            asset_type=_asset_type_of(other),
            version=getattr(other, "version", "") or "",
            score=score, band=band_of(score), reasons=reasons,
        ))

    # 동점은 record_id로 안정 정렬 — 같은 입력이면 같은 순서여야 테스트와 감사가 재현돼요.
    scored.sort(key=lambda c: (-c.score, c.record_id))
    return scored[:_MAX_CANDIDATES]


def _source_content(record) -> str:
    """소스 본문(Skill의 SKILL.md 등)을 읽어요. 읽을 수 없으면 "".

    Skill descriptors는 `{skill: {sourcePrefix}}`뿐이고 실제 내용은 소스 스토어에 있어요.
    이걸 읽지 않으면 능력 축이 항상 0이라, 같은 SKILL.md를 이름만 바꿔 등록해도 이름·설명
    유사도만으로 낮은 점수가 나와요(실측 28점).

    읽기 실패는 ""로 degrade해요 — 중복검토는 등록을 막지 않는 후처리라 여기서 예외를 올리면
    안 돼요.
    """
    prefix = getattr(record, "source_prefix", "") or ""
    parts = [seg for seg in prefix.split("/") if seg]
    if len(parts) < 4:
        return ""                     # 소스 비관리 자산(연결형 MCP·도메인 Agent)
    asset_type = parts[0]
    filename = _CONTENT_FILE.get(asset_type)
    if not filename:
        return ""                     # descriptor에 능력 정보가 있는 타입은 본문이 불필요해요
    asset_id, version = "/".join(parts[1:-1]), parts[-1]
    try:
        raw = get_source_store().read_file(asset_id, version, filename)
    except Exception:
        return ""
    if isinstance(raw, bytes):
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return ""
    return str(raw or "")


def overlap_view(record_id: str) -> dict:
    """중복검토 요약 — 큐 목록·게이트 상세·수동 수행 응답이 같은 형태로 써요.

    `state`로 "미검토"와 "검토했고 후보 0건"을 구분해요. 미검토를 후보 0건으로 보여주면
    검토자가 "중복 없음"이라는 없는 보장을 믿게 돼요(ScanState 4-enum과 같은 취지).

    이 모듈이 overlap의 소유자라 여기 둬요 — 라우터에 두면 다른 라우터가 private 함수를
    가져가면서 경계가 흐려져요.
    """
    store = get_gov_store()
    getter = getattr(store, "latest_overlap", None)
    empty = {"state": "not_reviewed", "count": 0, "band": None, "candidates": []}
    if getter is None:
        return empty
    try:
        overlap = getter(record_id)
    except Exception:
        return empty
    if overlap is None:
        return empty
    if overlap.status == "failed":
        return {**empty, "state": "failed", "error": overlap.error}
    if overlap.status != "done":
        return {**empty, "state": "reviewing"}
    candidates = list(overlap.candidates or [])
    return {
        "state": "reviewed",
        "count": len(candidates),
        "band": overlap.top_band if candidates else None,
        "candidates": candidates,
        "compared": overlap.compared,
        "reviewed_at": overlap.ts,
    }


def _lineage_of(record) -> str:
    """같은 논리 자산을 식별하는 키. 소스 관리형이 아니면 "".

    `source_prefix`가 `{type}/{owner}/{name}/{version}/`이라 **버전 세그먼트를 떼면** 버전을
    가로지르는 자산 신원이 돼요. 소스 비관리 자산(연결형 MCP·도메인 Agent)은 prefix가 없어서
    빈 문자열이고, 그때는 이 필터를 적용하지 않아요(그 자산들은 버전마다 새 record를 만들지
    않으니 애초에 이 문제가 없어요).
    """
    prefix = getattr(record, "source_prefix", "") or ""
    parts = [seg for seg in prefix.split("/") if seg]
    if len(parts) < 3:
        return ""
    return "/".join(parts[:-1])       # 버전 세그먼트 제거


def _sibling_version_reviewed(store, record_id: str) -> bool:
    """같은 논리 자산의 **다른 버전**이 이미 검토됐는지.

    source publish는 버전마다 새 Registry 레코드를 만들어서, 버전을 올릴 때마다 검토 기록이
    없는 새 레코드가 생겨요. 그대로 두면 정상 버전 업마다 전수 비교가 다시 돌아요 — 중복검토가
    답하는 질문("같은 일을 하는 다른 자산이 있나")의 답은 버전이 올라가도 바뀌지 않으니 낭비예요.

    소스 비관리 자산(연결형 MCP·도메인 Agent)은 lineage가 없어서 이 판단을 건너뜁니다 —
    그 자산들은 버전마다 새 레코드를 만들지 않아 애초에 이 문제가 없어요.

    조회 실패는 False로 degrade해요(검토를 돌리는 쪽). 건너뛰는 쪽으로 degrade하면 진짜 신규
    자산이 조용히 미검토로 남아요.
    """
    getter = getattr(store, "latest_overlap", None)
    if not callable(getter):
        return False
    try:
        registry, registry_id = get_registry(), get_registry_id()
        subject = registry.get_record(registry_id, record_id)
        lineage = _lineage_of(subject)
        if not lineage:
            return False
        for other in registry.list_records(registry_id):
            other_id = getattr(other, "record_id", "")
            if not other_id or other_id == record_id:
                continue
            if _lineage_of(other) != lineage:
                continue
            sibling = getter(other_id)
            if sibling is not None and sibling.status == "done":
                _log.info(
                    "같은 자산의 다른 버전이 이미 검토됐어요 — 버전 업은 재검토하지 않아요.",
                    extra={"record_id": record_id, "reviewed_sibling": other_id})
                return True
    except Exception:
        return False
    return False


def _should_review(latest: OverlapRecord | None, for_version: str) -> bool:
    """검토를 돌려야 하는지 — 기록이 없을 때만 돌려요.

    **버전 업은 재검토하지 않아요.** 중복검토가 답하는 질문은 "이 자산과 같은 일을 하는 다른
    자산이 이미 있나"예요. 같은 자산의 새 버전은 그 질문의 답을 바꾸지 않아요 — 이미 한 번
    검토했으면 소유자·능력·대상이 그대로고, 재검토는 같은 결과를 다시 계산할 뿐이에요.
    (보안 스캔은 반대예요 — 코드가 바뀌면 새 취약점이 들어올 수 있어서 `maybe_auto_scan`은
    `for_version`으로 재스캔해요. 두 축의 성격이 달라요.)

    `for_version`은 계약 호환을 위해 받되 판단에 쓰지 않아요. 재검토가 필요하면 관리자가
    중복검토 카드의 수행 버튼으로 직접 돌려요(`run_overlap_review`는 이 게이트를 안 거쳐요).

    실패한 검토는 다시 돌려요 — 실패 레코드를 "이미 검토함"으로 취급하면 일시 장애가 영구
    미검토로 굳어요.
    """
    if latest is None:
        return True
    if latest.status == "failed":
        return True
    return False


def _asset_type_of(record) -> str:
    """`descriptor_type`을 문자열로. enum·문자열·None을 모두 받아요."""
    value = getattr(record, "descriptor_type", None)
    if value is None:
        return ""
    return value.value if hasattr(value, "value") else str(value)
