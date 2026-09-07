import { AdminPlaceholder } from "@/components/admin/AdminPlaceholder";

// IH-163은 nav만 숨기고 이 호환용 라우트·placeholder는 남겨요.
// 폐기(ADR-0099): capability·connection을 중간층으로 계산하던 준비도 설계는 새 구현의
// 근거가 아니에요. 현행 호출 인가는 ④ agent tool binding과 ⑦ 사람·그룹 grant뿐이에요.
export default function AdminReadinessPage() {
  return (
    <AdminPlaceholder
      title="사전 준비 현황"
      description="호환용 placeholder예요. 현행 도구 호출 인가는 ④와 ⑦ 두 층으로 판정해요."
      planned={[
        "폐기(ADR-0099) — capability·connection 기반 준비도 설계 (새 구현 근거 아님)",
        "현행 ④ — agent tool binding 승인·자산 버전 대조 상태",
        "현행 ⑦ — 사람·그룹의 (asset, operation) grant 상태",
      ]}
    />
  );
}
