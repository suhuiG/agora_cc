"""런타임 도메인 — MCP·agent 구동 → 단일 게이트웨이 endpoint 제공.

오너: 런타임 도메인. AgentCore Runtime 배포 + 단일 게이트웨이 target 등록 + 호출 테스트.
게이트웨이 *코드*는 카탈로그 도메인가 소유하고, 런타임이 가져다 써요.

⚠️ 아직 구현 전이에요. 카탈로그(domains/catalog/)를 참조 구현으로 삼아 라우터를 채워요.
SoT가 필요하면 shared.deps 접근자나 카탈로그 port 로만 접근해요 (직접 put_item 금지).
"""
