"""레지스트리 논리 이름 상수.

과거엔 mock 부팅 시드(K-뷰티 데모 자산)를 여기 뒀지만, AWS 단일화로 시드를 제거했어요.
REGISTRY_NAME은 deps._build_backends()가 레지스트리 조회/생성 시 참조해요.
"""
from __future__ import annotations

REGISTRY_NAME = "agora-registry"
