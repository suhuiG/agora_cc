"use client";

import useSWR from "swr";
import { getCatalog, type AssetCard, type DescriptorType } from "@/lib/api";
import { cn } from "@/lib/ui";

// 플러그인 폼의 "멤버 자산" 선택기예요. descriptor_type(예: "Agent Skills" | "MCP") 하나를 받아
// 현재 Agora에 등록(APPROVED)된 자산을 체크박스 리스트로 보여줘요.
//
// getCatalog(type, 0, 100) 를 써요 — /api/catalog 는 APPROVED 카드 페이지를 돌려줘서
// searchAssets 의 max_results(≤50) 제약이나 빈 쿼리 이슈가 없어요.
export function AssetMemberPicker({
  type,
  label,
  selected,
  onToggle,
}: {
  type: DescriptorType;
  label: string;
  selected: string[];
  onToggle: (recordId: string) => void;
}) {
  const { data, error, isLoading } = useSWR<AssetCard[]>(
    ["bundle-asset-picker", type],
    () => getCatalog(type, 0, 100).then((page) => page.items),
  );

  const items = data ?? [];
  const selectedInList = items.filter((a) => selected.includes(a.record_id)).length;

  return (
    <div>
      <div className="mb-1.5 flex items-center justify-between">
        <span className="text-sm font-medium text-foreground">{label}</span>
        <span className="text-xs text-muted-foreground">
          {selectedInList > 0 ? `선택 ${selectedInList}개` : "선택 안 함"}
        </span>
      </div>

      <div className="max-h-56 overflow-auto rounded-lg border border-border bg-background/40">
        {isLoading ? (
          <div className="space-y-1.5 p-2">
            {Array.from({ length: 3 }).map((_, i) => (
              <div key={i} className="h-9 animate-pulse rounded-md bg-muted/60" />
            ))}
          </div>
        ) : error ? (
          <p className="px-3 py-3 text-xs text-red-600">
            {label} 목록을 불러오지 못했어요.
          </p>
        ) : items.length === 0 ? (
          <p className="px-3 py-3 text-xs text-muted-foreground">
            등록된 {label} 없음
          </p>
        ) : (
          <ul className="divide-y divide-border">
            {items.map((a) => {
              const checked = selected.includes(a.record_id);
              return (
                <li key={a.record_id}>
                  <label
                    className={cn(
                      "flex cursor-pointer items-start gap-3 px-3 py-2.5 transition-colors hover:bg-accent",
                      checked && "bg-accent/60",
                    )}
                  >
                    <input
                      type="checkbox"
                      checked={checked}
                      onChange={() => onToggle(a.record_id)}
                      className="mt-0.5 h-4 w-4 shrink-0 cursor-pointer rounded border-input accent-blue-600 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-1"
                    />
                    <span className="min-w-0 flex-1">
                      <span className="block truncate text-sm font-medium text-foreground">
                        {a.name}
                      </span>
                      <span className="block truncate font-mono text-[11px] text-muted-foreground">
                        {a.record_id}
                      </span>
                      {a.description?.trim() && (
                        <span className="mt-0.5 block line-clamp-1 text-xs text-muted-foreground">
                          {a.description}
                        </span>
                      )}
                    </span>
                  </label>
                </li>
              );
            })}
          </ul>
        )}
      </div>
    </div>
  );
}
