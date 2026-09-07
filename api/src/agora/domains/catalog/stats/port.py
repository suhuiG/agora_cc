"""StatsPort — 다운로드 집계 백엔드와 대화하는 단 하나의 인터페이스.

이 Port 뒤에 어떤 어댑터가 있든(FakeStatsStore / DynamoStatsStore) 레이어 코드는
동일해요. Task 2에서 DynamoStatsStore가 이 Protocol을 구현해요.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from .models import GroupedSnapshot, TopSnapshot, TypeGroup


@runtime_checkable
class StatsPort(Protocol):
    """다운로드 집계 포트. Fake/Dynamo 어댑터가 구현해요."""

    def increment_download(
        self,
        record_id: str,
        *,
        name: str,
        descriptor_type: str,
        owner_user: str = "",
    ) -> int:
        """record_id의 다운로드 수를 +1하고 갱신된 카운트를 반환해요.

        최초 호출이면 카운터를 0에서 시작해 1을 돌려줘요.
        name·descriptor_type·owner_user는 메타 저장용이에요(매번 덮어써서 최신 유지).
        카드 목록이 자산마다 Registry를 다시 조회하지 않게 여기 함께 담아요(N+1 회피).
        """
        ...

    def get_download_count(self, record_id: str) -> int:
        """record_id의 누적 다운로드 수를 반환해요. 없으면 0."""
        ...

    def top_downloads(self, limit: int = 5, *, force: bool = False) -> TopSnapshot:
        """내림차순 정렬 스냅샷을 반환해요.

        동점이면 record_id 오름차순으로 tiebreak해요.
        결과는 최대 limit개예요.

        force=True면 캐시 신선도를 무시하고 다시 집계해요(새로고침 버튼용).
        """
        ...

    def top_downloads_by_type(
        self, limit: int = 5, *, force: bool = False
    ) -> list[TypeGroup]:
        """타입마다 자기 안에서 상위 limit개를 골라 TypeGroup 목록을 반환해요.

        전체 톱N을 쪼개는 게 아니라 **타입마다 독립 순위**예요. 반환 길이·순서는
        항상 GROUP_ORDER와 같고, 다운로드 0건인 타입도 빈 그룹으로 들어와요
        (화면에서 빈 카드로 표시).
        """
        ...

    def top_downloads_grouped(
        self, limit: int = 5, *, force: bool = False
    ) -> GroupedSnapshot:
        """top_downloads_by_type + 계산 시각을 묶어 반환해요(API 응답용)."""
        ...

    def purge_record(self, record_id: str) -> None:
        """record_id의 카운터·메타를 제거해요. 없으면 noop."""
        ...

    def invalidate_snapshot(self) -> None:
        """캐시된 스냅샷을 무효화해요(다음 top_downloads 호출에서 재계산)."""
        ...
