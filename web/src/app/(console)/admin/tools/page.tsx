import { ToolsClient } from "@/components/admin/tools/ToolsClient";

// 거버넌스 도구 & 등급 관리 (§3-M3·M4).
// 데이터 로드·상호작용이 모두 클라이언트라 ToolsClient 로 위임해요
// (탭·SWR·등록/편집 패널·삭제 확인·권한 게이트).
export default function AdminToolsPage() {
  return <ToolsClient />;
}
