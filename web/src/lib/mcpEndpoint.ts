/**
 * 연결형 MCP endpoint 의 스킴 규칙 (CA-31).
 *
 * AgentCore Gateway 는 MCP server target endpoint 에 `https://` 만 받아요
 * (실측 2026-08-26: `CreateGatewayTarget` → `ValidationException ... Member must satisfy
 * regular expression pattern: https://.*`). 서버도 같은 규칙으로 거절하고(정본),
 * 여기서는 connect 조회 전에 먼저 알려줘 사용자가 마지막 단계에서 502 를 받지 않게 해요.
 */
export function mcpEndpointSchemeProblem(endpoint: string): string | null {
  const value = endpoint.trim();
  if (!value) return null; // 빈 값은 다른 검사가 다뤄요.
  if (value.toLowerCase().startsWith("https://")) return null;
  if (value.toLowerCase().startsWith("http://")) {
    return (
      "endpoint 는 `https://` 여야 해요 — AgentCore Gateway 가 MCP server target 에 " +
      "https 만 허용해요. TLS 로 공개된 주소를 넣어 주세요. " +
      "(로컬 MCP 는 연결형으로 등록할 수 없어요 — 배포형을 쓰세요.)"
    );
  }
  return "endpoint 는 `https://` 로 시작하는 URL 이어야 해요.";
}
