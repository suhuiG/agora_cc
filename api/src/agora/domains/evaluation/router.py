"""평가 도메인 라우터 (스켈레톤).

오너: 미배정. 라우트를 추가한 뒤, server.py 에서 include_router 한 줄을 활성화해요.
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(tags=["evaluation"])

# TODO: 품질 점수 · 트레이스·메트릭 · 비용 라우트.
