import { ConsoleHomeClient } from "@/components/admin/home/ConsoleHomeClient";

// 콘솔 홈 (/admin) — 승인 큐는 /admin/queue 로 독립했어요.
export default function AdminHomePage() {
  return <ConsoleHomeClient />;
}
