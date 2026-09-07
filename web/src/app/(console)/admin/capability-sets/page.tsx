import { CapSetsClient } from "@/components/admin/capability-sets/CapSetsClient";

// 권한 그룹 정의 — 기존 access/ 의 connections 탭을 분리·재정의했어요 (S2).
export default function AdminCapabilitySetsPage() {
  return <CapSetsClient />;
}
