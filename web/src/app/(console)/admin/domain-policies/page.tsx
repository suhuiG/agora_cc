import { DomainPolicyClient } from "@/components/admin/domain-policies/DomainPolicyClient";

// 도메인 정책 (Cedar) — 요청 인자 값에 걸는 규칙이에요 (IH-132).
// "Gateway 정책" 화면과 다른 화면이에요: 그쪽은 라이브 Cedar 전부를 훑는 감사 화면이고,
// 이쪽은 도메인 규칙 하나를 만드는 작업 화면이에요.
export default function AdminDomainPoliciesPage() {
  return <DomainPolicyClient />;
}
