"""다운로드 통계 도메인 모델 — 순수 데이터 클래스, 외부 의존 없음."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class TopEntry:
    """톱5 목록 단건 항목.

    record_id:       레지스트리 레코드 식별자.
    name:            레코드 이름(표시용).
    descriptor_type: 자산 타입 문자열(MCP/Skill/Agent 등).
    downloads:       누적 다운로드 수.
    owner_user:      등록자(자산을 올린 사람). 카드 오른쪽에 표시해요.
                     미상이면 빈 문자열이고, 화면에서 '—'로 폴백해요.
    """

    record_id: str
    name: str
    descriptor_type: str
    downloads: int
    owner_user: str = ""


@dataclass
class TopSnapshot:
    """특정 시점에 계산된 톱 다운로드 스냅샷.

    computed_at: ISO 8601 UTC 문자열(KST 변환은 표현 계층에서).
    items:       내림차순 정렬된 TopEntry 목록.
    """

    computed_at: str
    items: list[TopEntry] = field(default_factory=list)


# 카드로 보여줄 자산 타입 순서. 카탈로그 섹션 순서(web assetTypes.SECTION_ORDER)와
# 같아요 — 화면에서 위쪽 카드와 아래쪽 섹션이 같은 순서로 흐르게요.
# 여기 없는 타입(App·Model·Custom)은 카드가 없으니 집계에서 제외해요.
GROUP_ORDER: tuple[str, ...] = ("Agent", "MCP", "Agent Skills")


@dataclass
class TypeGroup:
    """한 자산 타입의 톱N 묶음 — 화면의 카드 하나에 대응해요.

    descriptor_type: 자산 타입 문자열.
    items:           그 타입 안에서 내림차순 정렬된 TopEntry(최대 limit개).
                     다운로드가 0건인 타입은 빈 목록이에요(빈 카드로 표시).
    """

    descriptor_type: str
    items: list[TopEntry] = field(default_factory=list)


@dataclass
class GroupedSnapshot:
    """타입별 카드 전체 + 계산 시각.

    groups는 항상 GROUP_ORDER 순서·길이예요(빈 타입도 포함).
    """

    computed_at: str
    groups: list[TypeGroup] = field(default_factory=list)
