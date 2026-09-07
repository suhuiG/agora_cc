"""JsonBundleStore — Bundle을 로컬 JSON에 영속화(AuxStore·GovStore 패턴).

DDB 이전은 후속. 프로덕션 배선은 shared/deps.get_bundle_store()가 소유.
"""
from __future__ import annotations

import json
from pathlib import Path

from .models import Bundle


class JsonBundleStore:
    def __init__(self, store_path: str | Path | None = None) -> None:
        self._store_path = Path(store_path) if store_path else None
        self._bundles: dict[str, Bundle] = {}
        if self._store_path and self._store_path.exists():
            self._load()

    def put(self, bundle: Bundle) -> None:
        self._bundles[bundle.bundle_id] = bundle
        self._persist()

    def get(self, bundle_id: str) -> Bundle | None:
        return self._bundles.get(bundle_id)

    def list(self) -> list[Bundle]:
        return sorted(self._bundles.values(), key=lambda b: b.updated_at, reverse=True)

    def delete(self, bundle_id: str) -> bool:
        existed = self._bundles.pop(bundle_id, None) is not None
        if existed:
            self._persist()
        return existed

    def remove_member_everywhere(self, record_id: str) -> int:
        """모든 번들에서 record_id 멤버십을 제거해요. 제거가 일어난 번들 수 반환(하드 삭제 정리용)."""
        n = 0
        for bundle in self._bundles.values():
            if record_id in bundle.member_ids:
                bundle.member_ids = [m for m in bundle.member_ids if m != record_id]
                n += 1
        if n:
            self._persist()
        return n

    def _persist(self) -> None:
        if not self._store_path:
            return
        payload = {bid: b.to_dict() for bid, b in self._bundles.items()}
        self._store_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))

    def _load(self) -> None:
        raw = json.loads(self._store_path.read_text())
        for bid, d in raw.items():
            self._bundles[bid] = Bundle.from_dict(d)
