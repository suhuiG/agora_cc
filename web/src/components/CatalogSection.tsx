"use client";

import useSWRInfinite from "swr/infinite";
import { getCatalog, type AssetCard, type CatalogPage, type DescriptorType } from "@/lib/api";
import { ASSET_TYPE_META } from "@/lib/assetTypes";
import { Card } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";

const PAGE = 6;

export function CatalogSection({
  type,
  label,
  onInstall,
}: {
  type: DescriptorType;
  label: string;
  onInstall: (asset: AssetCard) => void;
}) {
  // 각 페이지의 SWR 키: [경로, type, offset, limit]. 이전 페이지가 마지막이면 null(정지).
  const getKey = (pageIndex: number, prev: CatalogPage | null) => {
    if (prev && prev.offset + prev.items.length >= prev.total) return null;
    return ["/api/catalog", type, pageIndex * PAGE, PAGE] as const;
  };
  const { data, size, setSize, isLoading } = useSWRInfinite<CatalogPage>(
    getKey,
    (key) => getCatalog(key[1] as DescriptorType, key[2] as number, key[3] as number),
  );

  const pages = data ?? [];
  const items = pages.flatMap((p) => p.items);
  const total = pages.length > 0 ? pages[0].total : 0;
  const hasMore = items.length < total;
  const meta = ASSET_TYPE_META[type];

  if (!isLoading && items.length === 0) return null; // 빈 섹션 스킵

  return (
    <section>
      <div className="mb-4 flex items-center gap-2 border-b border-border pb-2">
        <span className="text-base font-semibold">{label}</span>
        {total > 0 && <span className="text-sm text-muted-foreground">{total}개</span>}
      </div>
      <div className="grid grid-cols-1 gap-5 md:grid-cols-2 lg:grid-cols-3">
        {items.map((asset) => (
          <a key={asset.record_id} href={`/catalog/assets/${asset.record_id}`} className="group block">
            <Card className="flex h-full flex-col transition-all group-hover:border-slate-300 group-hover:shadow-md">
              <div className="flex flex-1 flex-col p-5">
                <div className="mb-3 flex items-center justify-between">
                  {meta && <Badge variant="type" className={meta.pill}>{meta.label}</Badge>}
                  <span className="text-xs text-muted-foreground">v{asset.version}</span>
                </div>
                <h3 className="mb-1 font-semibold text-card-foreground">{asset.name}</h3>
                <p className="mb-3 line-clamp-2 text-sm text-muted-foreground">
                  {asset.description?.trim() || "설명이 없어요"}
                </p>
                <div className="mb-3 flex flex-wrap gap-1">
                  {(asset.tags || []).slice(0, 3).map((tag) => (
                    <Badge key={tag} variant="tag">{tag}</Badge>
                  ))}
                </div>
                <div className="mt-auto flex items-center justify-between text-xs text-muted-foreground">
                  <span>{asset.category} · {asset.owner_team}</span>
                  <span className="flex items-center gap-2">
                    {asset.views > 0 && <span>{asset.views} views</span>}
                  </span>
                </div>
                {asset.descriptor_type === "Agent Skills" && asset.source_prefix && (
                  <Button
                    type="button"
                    size="sm"
                    className="mt-3 self-start"
                    onClick={(e) => {
                      e.preventDefault();
                      e.stopPropagation();
                      onInstall(asset);
                    }}
                  >
                    설치
                  </Button>
                )}
              </div>
            </Card>
          </a>
        ))}
      </div>
      {hasMore && (
        <div className="mt-5 flex justify-center">
          <Button variant="outline" onClick={() => setSize(size + 1)}>더보기</Button>
        </div>
      )}
    </section>
  );
}
