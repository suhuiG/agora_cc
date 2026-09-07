import { BundlesClient } from "@/components/admin/bundles/BundlesClient";
import { ReviewQueue } from "@/components/admin/bundles/ReviewQueue";

export default function AdminBundlesPage() {
  return (
    <>
      {/* 사용자 배포 신청 검토 큐 — 대기 건이 없으면 아무것도 렌더하지 않아요. */}
      <ReviewQueue />
      <BundlesClient />
    </>
  );
}
