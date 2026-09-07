import { ReviewClient } from "@/components/admin/queue/ReviewClient";

// 자산 상세 판정 (§3-M2) — 승인 큐에서 자산명 클릭 시 진입.
// 사이드바 메뉴가 아닌 큐의 하위 경로예요.
export default async function AssetReviewPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  return <ReviewClient recordId={id} />;
}
