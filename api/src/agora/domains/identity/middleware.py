"""모든 Agora API 요청의 사용자 신원을 입구에서 확립해요."""
from __future__ import annotations

from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from .service import IdentityService
from .token_verifier import AuthenticationError, AuthorizationError

_PUBLIC_PATHS = frozenset({
    "/api/health",
    # This endpoint authenticates its opaque dev credential itself. It cannot
    # satisfy the human Cognito principal middleware; no sibling route is public.
    "/api/dev-identity/token",
    # 같은 이유예요 — 이 경로도 opaque dev credential 을 직접 인증해요. 사람 Cognito
    # principal 이 없어서 미들웨어를 만족시킬 수 없어요.
    "/api/dev-identity/call-handle",
})


class IdentityMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, *, service: IdentityService | None = None):
        super().__init__(app)
        self._service = service or IdentityService.from_env()

    async def dispatch(self, request, call_next):
        if (
            request.method == "OPTIONS"
            or not request.url.path.startswith("/api/")
            or request.url.path in _PUBLIC_PATHS
        ):
            return await call_next(request)
        try:
            request.state.principal = self._service.authenticate(request.headers)
        except AuthenticationError as exc:
            return JSONResponse(
                {"detail": str(exc)},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
        except AuthorizationError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=403)
        return await call_next(request)
