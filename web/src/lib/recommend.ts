// 자산의 이름·설명 텍스트에서 카테고리·태그를 규칙 기반으로 추천해요.
// LLM 호출 없이 키워드 매칭만 써요(가볍고 예측 가능). 등록/수정 폼의 "추천 채우기"에 사용.

// 카테고리 추천 규칙: 키워드가 하나라도 매칭되면 그 카테고리를 제안해요(위에서부터 우선).
const CATEGORY_RULES: { category: string; keywords: string[] }[] = [
  { category: "Documentation", keywords: ["doc", "documentation", "문서", "guide", "reference", "manual"] },
  { category: "Search", keywords: ["search", "검색", "query", "find", "retrieval"] },
  { category: "Data", keywords: ["data", "database", "db", "sql", "table", "데이터", "query", "analytics"] },
  { category: "DevOps", keywords: ["deploy", "ci", "cd", "pipeline", "infra", "cloudformation", "terraform", "배포"] },
  { category: "Security", keywords: ["security", "auth", "iam", "credential", "보안", "인증", "권한"] },
  { category: "Productivity", keywords: ["note", "obsidian", "task", "todo", "calendar", "노트", "일정", "생산성"] },
  { category: "Communication", keywords: ["slack", "email", "message", "chat", "메시지", "메일", "알림"] },
  { category: "AI/ML", keywords: ["llm", "model", "embedding", "agent", "bedrock", "inference", "ai", "ml"] },
];

// 태그 추천 규칙: 매칭되는 키워드를 태그로 제안해요(여러 개 가능).
const TAG_KEYWORDS = [
  "aws", "bedrock", "mcp", "documentation", "search", "database", "sql",
  "slack", "email", "obsidian", "security", "iam", "agent", "llm",
  "translation", "analytics", "monitoring", "deploy",
];

export type Recommendation = { category: string; tags: string[] };

/** 이름·설명·(선택)tool명들에서 카테고리 1개 + 태그 여러 개를 추천해요. */
export function recommendCurations(
  name: string,
  description: string,
  toolNames: string[] = [],
): Recommendation {
  const haystack = [name, description, ...toolNames].join(" ").toLowerCase();

  let category = "";
  for (const rule of CATEGORY_RULES) {
    if (rule.keywords.some((k) => haystack.includes(k))) {
      category = rule.category;
      break;
    }
  }

  const tags: string[] = [];
  for (const kw of TAG_KEYWORDS) {
    if (haystack.includes(kw) && !tags.includes(kw)) tags.push(kw);
  }

  return { category, tags: tags.slice(0, 5) };
}
