import { GrantsClient } from "@/components/admin/grants/GrantsClient";

// 사용자 권한 — 기존 access/ 의 grants 탭에서 분리했어요 (S2).
export default function AdminGrantsPage() {
  return <GrantsClient />;
}
