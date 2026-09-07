import { Suspense } from "react";
import { QueueClient } from "@/components/admin/queue/QueueClient";

// 승인 큐 (§3-M1) — 콘솔 홈(/admin)에서 독립한 전용 경로예요.
// 데이터 로드·상호작용이 클라이언트라 QueueClient 로 위임해요.
export default function AdminQueuePage() {
  return (
    <Suspense fallback={<div className="py-16 text-center text-muted-foreground">불러오는 중...</div>}>
      <QueueClient />
    </Suspense>
  );
}
