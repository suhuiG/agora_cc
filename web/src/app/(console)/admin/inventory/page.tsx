import { InventoryClient } from "@/components/admin/inventory/InventoryClient";

// 자산 인벤토리 (W5) — 소유·상태·중복 관점. 기존 대시보드(심사 관측 축)와 별개 화면이에요.
export default function AdminInventoryPage() {
  return <InventoryClient />;
}
