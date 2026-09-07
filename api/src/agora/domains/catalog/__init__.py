"""카탈로그 도메인 — 자산 발견·상세·퍼블리시 + SoT(AgoraCatalog) 소유.

오너: 카탈로그 도메인.
- registry/    : RegistryPort + Mock/Dynamo/AWS 어댑터 (카탈로그 SoT)
- sourcestore/ : SourceStorePort + 버전·감사·바인딩
- aux_store    : 리뷰·조회수 (Registry 미수용 부가 데이터)
- router       : 발견·상세·리뷰·퍼블리시·소스 API
- schemas      : 요청·응답 모델

다른 도메인은 이 도메인의 port(RegistryPort/SourceStorePort)나 shared.deps 접근자로만
카탈로그에 접근해요.
"""
