import { ToolAccessClient } from "@/components/admin/tool-access/ToolAccessClient";

// 도구별 «호출 주체» 관리 — ⑦층(ADR-0099 결정 7, IH-145).
// `/admin/tool-authorization` 과 축이 달라요 — 저쪽은 ④층(agent 의 신청 승인)이에요.
export default function AdminToolAccessPage() {
  return <ToolAccessClient />;
}
