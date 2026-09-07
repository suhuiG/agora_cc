"""배포 Runtime 전용 authorization HTTP 진입점.

사람용 API와 같은 identity 도메인을 사용하되, 외부에는 workload decision route만
노출해 공격 표면과 Lambda cold start를 줄여요.
"""
from __future__ import annotations

from fastapi import FastAPI
from mangum import Mangum

from .domains.identity.access_router import internal_router

app = FastAPI(title="Agora Runtime Authorization", docs_url=None, redoc_url=None)
app.include_router(internal_router)

handler = Mangum(app, lifespan="off")
