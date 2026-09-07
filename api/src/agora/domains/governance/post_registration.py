"""등록 후 거버넌스 후처리 단일 진입점 — 자동 스캔 + 중복검토.

**왜 한 함수인가.** ADR-017의 배경이 정확히 이거예요: 등록 경로마다 거버넌스 호출을 인라인으로
복제했더니 5개 경로 중 2개에만 적용돼 있었어요. 후처리가 둘(스캔·중복검토)로 늘어나면 경로×후처리
= 10개 지점이 되고, 새 후처리를 추가할 때 누락이 반복돼요. 등록 경로는 이 함수 하나만 부르고,
어떤 후처리가 도는지는 여기서만 결정해요.

호출 순서 계약 (ADR-017 결정 1 연장):

    create_record → run_registration_hook → **run_post_registration** → 성공 응답

두 후처리 모두 비차단이에요. 하나가 실패해도 다른 하나는 돌고, 둘 다 실패해도 등록은 성공해요.
"""
from __future__ import annotations


def run_post_registration(record_id: str, *, for_version: str = "") -> dict:
    """등록 후 거버넌스 후처리를 모두 실행해요. 각 후처리의 실행 여부를 dict로 돌려줘요.

    `for_version`은 재배포 판별용이에요 — 두 후처리 모두 "최신 기록이 이 버전을 봤는가"로
    멱등을 판단하므로 같은 값을 그대로 넘겨요.

    반환값은 관측용이에요. 호출부가 이걸 보고 분기하지는 않아요(후처리 실패로 등록을 되돌리지
    않는 게 계약이니까요).

    **여기서 예외를 밖으로 내보내지 않아요.** 각 후처리는 자체 try/except를 갖지만 그 안쪽만
    감싸고 있어서, 설정 조회(`get_settings`)나 최신 기록 조회(`latest_*`)가 일시 실패하면
    예외가 여기까지 올라와요. 그러면 이미 durable하게 저장된 등록이 HTTP 500이 되고, 클라이언트는
    성공 ID를 못 받은 채 같은 이름으로 재시도해 unique key 충돌을 만나요(복구 어려운 부분 성공).
    후처리는 다음 등록·재배포 poll이 멱등하게 재시도하므로 삼키는 게 맞아요.

    두 후처리를 **각각** 격리하는 이유: 한 dict 리터럴에 나란히 두면 값이 순서대로 평가돼서
    첫 후처리 예외가 두 번째를 아예 실행되지 못하게 해요(둘은 독립 축이에요).
    """
    from .auto_scan import maybe_auto_scan
    from .overlap_review import maybe_overlap_review

    return {
        "scanned": _safely(maybe_auto_scan, record_id, for_version, "자동 스캔"),
        "overlap_reviewed": _safely(
            maybe_overlap_review, record_id, for_version, "중복검토"),
    }


def run_overlap_only(record_id: str, *, for_version: str = "") -> bool:
    """중복검토만 실행해요(스캔 면제 경로용). 같은 예외 격리를 거쳐요.

    Initializr 배포처럼 security scan을 의도적으로 건너뛰는 경로가 쓰는 진입점이에요.
    `maybe_overlap_review`를 직접 부르면 예외 격리를 우회해 배포 poll이 500이 돼요.
    """
    from .overlap_review import maybe_overlap_review

    return _safely(maybe_overlap_review, record_id, for_version, "중복검토")


def _safely(processor, record_id: str, for_version: str, label: str) -> bool:
    """후처리 하나를 실행하고 예외를 삼켜요. 실패는 False."""
    import logging

    try:
        return processor(record_id, for_version=for_version)
    except Exception:
        logging.getLogger(__name__).warning(
            "등록 후처리(%s)가 실패했어요 — 등록은 유지하고 다음 순회가 재시도해요.",
            label, extra={"record_id": record_id}, exc_info=True)
        return False
