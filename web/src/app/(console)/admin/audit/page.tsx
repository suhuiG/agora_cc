import { AuditClient } from "@/components/admin/audit/AuditClient";

// 감사 로그 (§3-M6) — 판정 이력 조회(읽기 전용, append-only).
export default function AdminAuditPage() {
  return <AuditClient />;
}
