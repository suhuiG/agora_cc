import { redirect } from "next/navigation";

// 구 URL(/governance/admin/*) → 신 URL(/admin/*) 이전용 shim.
// 이미 배포된 경로라 북마크·문서 링크가 살아 있어서 404 를 내지 않고 넘겨요.
//
// optional catch-all([[...path]]) 이라 /governance/admin 자체도 이 페이지가 받아요.
// Next 16 에서 params 는 Promise 예요 — 동기 접근은 오류라 반드시 await 해요.
//
// 307(기본 redirect)을 써요. permanentRedirect(308)는 브라우저가 캐시해서
// 경로를 되돌릴 여지가 사라지거든요.
const MOVED: Record<string, string> = {
  // access 탭은 grants·capability-sets 두 화면으로 쪼개졌어요. 기본값을 grants 로 둬요.
  access: "grants",
};

export default async function LegacyAdminRedirect({
  params,
}: {
  params: Promise<{ path?: string[] }>;
}) {
  const { path = [] } = await params;
  const [head, ...rest] = path;
  const target = head ? [MOVED[head] ?? head, ...rest] : [];
  redirect(`/admin${target.length ? `/${target.join("/")}` : ""}`);
}
