import { Suspense } from "react";
import { CatalogGrid } from "@/components/CatalogGrid";

// 카탈로그 > 둘러보기. CatalogGrid 가 useSearchParams 를 쓰므로 Suspense 로 감싸요.
export default function BrowsePage() {
  return (
    <Suspense fallback={<div className="py-16 text-center text-muted-foreground">불러오는 중...</div>}>
      <CatalogGrid />
    </Suspense>
  );
}
