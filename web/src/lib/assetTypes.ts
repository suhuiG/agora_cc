// 자산 타입(descriptor_type) 메타 — 카탈로그·상세 페이지가 공유하는 단일 출처예요.
// UI 원칙: pill(라운드 배지)은 태그/타입 칩 표현에만 써요.
//
// 백엔드 DescriptorType(api/src/agora/registry/models.py)와 1:1:
//   MCP · Agent · Agent Skills · App · Model · Custom

export interface AssetTypeMeta {
  /** 화면 표시 라벨 */
  label: string;
  /** 타입 칩 pill 색 (Tailwind 유틸 클래스) */
  pill: string;
  /** 한 줄 성격 설명 (상세 페이지 등에서 보조 노출) */
  blurb: string;
}

export const ASSET_TYPE_META: Record<string, AssetTypeMeta> = {
  Agent: {
    label: "Agent",
    pill: "bg-purple-100 text-purple-800",
    blurb: "LLM·도구를 호출하는 에이전트. 런타임에서 구동돼 endpoint로 제공돼요.",
  },
  MCP: {
    label: "MCP Tool",
    pill: "bg-blue-100 text-blue-800",
    blurb: "MCP 서버/도구. 런타임에서 구동돼 게이트웨이 endpoint로 호출해요.",
  },
  "Agent Skills": {
    label: "Skill",
    pill: "bg-emerald-100 text-emerald-800",
    blurb: "로컬에 설치하는 마크다운 스킬. 구동 없이 설치 명령으로 바로 써요.",
  },
  App: {
    label: "App",
    pill: "bg-amber-100 text-amber-800",
    blurb: "LLM 호출이 없는 일반 앱. 별도 호스팅으로 배포돼 endpoint로 제공돼요.",
  },
  Model: {
    label: "Model",
    pill: "bg-rose-100 text-rose-800",
    blurb: "Bedrock 등이 호스팅하는 모델 참조. 플레이그라운드에서 선택해 써요.",
  },
  Custom: {
    label: "Custom",
    pill: "bg-slate-100 text-slate-700",
    blurb: "커스텀 스키마 자산이에요.",
  },
};

// 카탈로그 타입 필터 드롭다운 옵션 (빈 값 = 전체)
// App·Model은 화면에서 제외해요 — ASSET_TYPE_META에는 남겨둬서 기존 자산이 생겨도
// 배지·설명은 정상 렌더돼요(백엔드 DescriptorType도 그대로 유지).
export const TYPE_FILTER_OPTIONS = ["MCP", "Agent Skills", "Agent"] as const;

// 카탈로그 섹션 표시 순서 (우선순위: agent → MCP → skill)
export const SECTION_ORDER: { type: string; label: string }[] = [
  { type: "Agent", label: "Agent" },
  { type: "MCP", label: "MCP Tool" },
  { type: "Agent Skills", label: "Skill" },
];
