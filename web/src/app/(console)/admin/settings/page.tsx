import { SettingsClient } from "@/components/admin/settings/SettingsClient";

// 콘솔 설정 (§3-M7) — 자동 스캔 토글 + 릴리스 자동감지 주기.
// 데이터 로드·상호작용이 클라이언트라 SettingsClient 로 위임해요.
export default function AdminSettingsPage() {
  return <SettingsClient />;
}
