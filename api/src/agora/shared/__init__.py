"""shared — 모든 도메인이 공용으로 쓰는 계층.

- config: 환경변수 단일 로더 (docs/05-operations.md §5 계약).
- deps:   DI 싱글톤 + 접근자 (RegistryPort 어댑터·AuxStore·SourceStorePort).
- slug:   asset_id 세그먼트 슬러그화 헬퍼.

도메인 라우터는 이 계층의 접근자만 import 해요. 도메인끼리 직접 import 하지 않아요.
"""
