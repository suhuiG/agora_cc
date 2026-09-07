"""타입별 톱N 그룹핑 — 순수함수. Fake·Dynamo 어댑터가 함께 써요.

카운터 전량(record_id·name·descriptor_type·downloads)을 받아, 타입마다 자기 안에서
상위 N개를 골라요. 전체 톱N을 쪼개는 게 아니라 **타입마다 독립 순위**예요.
"""
from __future__ import annotations

from .models import GROUP_ORDER, TopEntry, TypeGroup


def group_by_type(entries: list[TopEntry], *, limit: int) -> list[TypeGroup]:
    """타입마다 내림차순 상위 limit개를 담은 TypeGroup 목록을 반환해요.

    - 반환 길이·순서는 항상 GROUP_ORDER와 같아요(다운로드 0건 타입도 빈 그룹으로).
    - GROUP_ORDER에 없는 타입(Custom 등)은 카드가 없으니 버려요.
    - 동점은 record_id 오름차순으로 tiebreak해요(결정적 순서).
    """
    buckets: dict[str, list[TopEntry]] = {t: [] for t in GROUP_ORDER}
    for entry in entries:
        bucket = buckets.get(entry.descriptor_type)
        if bucket is None:
            continue          # 카드 없는 타입 — 집계 제외
        bucket.append(entry)

    return [
        TypeGroup(
            descriptor_type=dtype,
            items=sorted(
                buckets[dtype],
                key=lambda e: (-e.downloads, e.record_id),
            )[:limit],
        )
        for dtype in GROUP_ORDER
    ]
