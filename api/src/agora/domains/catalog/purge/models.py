"""PurgeReport — 하드 삭제 결과 리포트(best-effort 정리의 단계별 결과)."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class PurgeReport:
    deleted: list[str] = field(default_factory=list)      # 성공 정리한 저장소/리소스 라벨
    failed: list[dict] = field(default_factory=list)      # {"store": str, "reason": str}
    skipped: list[str] = field(default_factory=list)      # 미지원(정리 API 없음) 라벨

    def to_dict(self) -> dict:
        return {"deleted": list(self.deleted),
                "failed": [dict(f) for f in self.failed],
                "skipped": list(self.skipped)}
