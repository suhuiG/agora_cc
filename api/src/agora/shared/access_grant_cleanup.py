"""자산 purge 가 ⑦ access grant 를 회수하는 cross-domain 계약이에요.

⑤ 의 `asset_capability_cleanup` 과 같은 모양이에요 — catalog 는 Registry 좌표(record_id)만
넘기고, `AuditEvent` 조립과 삭제는 identity 가 소유해요. catalog 가 `identity.models` 를 직접
import 하면 도메인 경계가 깨지거든요(AGENTS.md §Architecture And Ownership).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

#: 보고 좌표 한 칸 — `(subject, asset_id, operation_id)` 예요. `subject` 는 `group:<name>`
#: 또는 `principal:<sub>` 라벨이에요. ⑦ 는 **주체가 파티션 키**라서 주체를 빼면 어느 행이
#: 남았는지 다시 찾을 수 없어요.
GrantCoordinate = tuple[str, str, str]


@dataclass(frozen=True)
class AccessGrantCleanupReport:
    """⑦ 정리 결과예요.

    **`observed` 에 기본값이 없는 건 일부러예요.** 「0행 찾음」과 「조회 못 함」은 다른
    사실이고, 기본값을 주면 아무 말도 하지 않은 생산자가 관측 성공으로 읽혀요 — 미관측을
    통과로 기록하는 것과 같아요(AGENTS.md §Gates must not validate themselves).
    """

    observed: bool
    deleted: tuple[GrantCoordinate, ...] = ()
    #: 지우지 못했거나 부재를 확인하지 못한 좌표예요. 비어 있지 않으면 호출자는 **실패로
    #: 다뤄야 해요** — Registry 레코드가 재시도 좌표의 유일한 소유자거든요.
    unknown: tuple[GrantCoordinate, ...] = ()
    reason: str = ""


class AccessGrantCleaner(Protocol):
    def delete_record_grants(
        self,
        record_ids: tuple[str, ...],
        *,
        actor: str,
    ) -> AccessGrantCleanupReport: ...
