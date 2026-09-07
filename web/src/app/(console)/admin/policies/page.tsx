import { AgentPolicyInventoryClient } from "@/components/governance/policies/AgentPolicyInventoryClient";

// 이 라우트는 `(console)/admin/layout.tsx` 아래라 상위 layout의 AdminGuard가 이미 감싸요.
// page-level guard를 겹치지 않아요. AdminGuard는 클라이언트 UX 체크일 뿐 접근 제어가 아니고,
// 실제 강제는 inventory API의 `require_role("admin")`가 담당해요.
export default function AdminPoliciesPage() {
  return <AgentPolicyInventoryClient />;
}
