# domains/ — 도메인별 백엔드

도메인 = 협업 경계. 각자 자기 도메인 폴더 안에서만 작업해요.
운영은 단일 FastAPI 앱이고, 여기 폴더 경계가 곧 오너십 경계예요 (ADR-003·006).

## 도메인 목록

| 도메인 | 폴더 | 역할 |
|---|---|---|
| 카탈로그 | `catalog/` | 자산 등록·레지스트리 SoT·MCP 드리프트 |
| 거버넌스 | `governance/` | 보안 스캔·게이트 판정·승인 워크플로우 |
| 런타임 | `runtime/` | MCP·agent 배포 파이프라인 |
| 아이덴티티 | `identity/` | 사람·agent 인증, 도구 인가 원장, Cedar 정책 |
| 플레이그라운드 | `playground/` | 배포된 agent 호출·관측 |
| 모니터링 | `monitoring/` | 텔레메트리 집계 |
| 번들 | `bundle/` | 배포 번들 내보내기 |
| 평가 | `evaluation/` | 자산 평가 |

## 새 라우트를 추가하려면

1. 자기 도메인 폴더의 `router.py`에 핸들러를 추가해요 (`catalog/router.py`가 참조 구현).
2. 백엔드(SoT·소스 스토어·리뷰)가 필요하면 **`shared.deps` 접근자**로만 가져와요:
   ```python
   from ...shared.deps import get_registry, get_registry_id, get_source_store
   ```
   직접 어댑터를 생성하거나 `put_item` 하지 않아요 — 카탈로그가 SoT를 소유해요 (ADR-004).
3. 라우터가 처음 라우트를 가지면 `server.py`에 한 줄 추가해요:
   ```python
   from .domains.runtime.router import router as runtime_router
   app.include_router(runtime_router)
   ```

## 규칙

- **도메인끼리 직접 import 금지.** 공유가 필요하면 `shared/`로 올리거나 카탈로그 port를 써요.
- **요청·응답 모델**은 자기 도메인의 `schemas.py`에. (카탈로그는 `catalog/schemas.py`)
- 환경변수는 `shared/config.py` 단일 로더로만 읽어요.
