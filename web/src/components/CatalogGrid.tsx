"use client";

import { useState } from "react";
import { useSearchParams } from "next/navigation";
import useSWR from "swr";
import {
  getInstallInstruction,
  detectOs,
  type AssetCard,
  type InstallInstruction,
  type SearchResult,
  type DescriptorType,
} from "@/lib/api";
import { ASSET_TYPE_META, SECTION_ORDER, TYPE_FILTER_OPTIONS } from "@/lib/assetTypes";
import { CatalogSection } from "@/components/CatalogSection";
import { TopDownloads } from "@/components/catalog/TopDownloads";
import { Card } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Select } from "@/components/ui/input";
import { Modal } from "@/components/ui/modal";
import { useToast } from "@/components/ui/toast";

export function CatalogGrid() {
  const params = useSearchParams();
  const q = params.get("q")?.trim() ?? "";

  const [typeFilter, setTypeFilter] = useState("");
  const [install, setInstall] = useState<InstallInstruction | null>(null);
  const [copied, setCopied] = useState(false);
  const { success, error: toastError } = useToast();

  // 검색 모드: q가 있으면 /api/search를 SWR로. (검색은 랭킹순 단일 리스트)
  const searchParams = new URLSearchParams();
  if (q) searchParams.set("q", q);
  if (typeFilter) searchParams.set("type", typeFilter);
  const searchUrl = `/api/search?${searchParams.toString()}`;
  const { data: searchResults, isLoading: searchLoading } = useSWR<SearchResult[]>(
    q ? ["/api/search", q, typeFilter] : null,
    () => fetch("/api/backend" + searchUrl).then((r) => {
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      return r.json();
    }),
  );

  async function openInstall(asset: AssetCard | SearchResult) {
    try {
      const ins = await getInstallInstruction(asset.record_id, { tool: "claude", os: detectOs() });
      setInstall(ins);
      setCopied(false);
    } catch {
      // 예전엔 조용히 넘겼는데, 버튼이 죽은 것처럼 보여서 알림을 띄워요.
      toastError("설치 명령을 불러오지 못했어요.", "자산 상세에서 다시 시도해 주세요.");
    }
  }

  async function copyInstall() {
    if (install?.command) {
      try {
        await navigator.clipboard.writeText(install.command);
      } catch {
        // 권한 거부·비보안 컨텍스트 — 조용히 실패하면 버튼이 죽은 것처럼 보여요.
        toastError(
          "설치 명령을 복사하지 못했어요.",
          "브라우저가 클립보드 접근을 막았어요. 직접 선택해 복사해 주세요.",
        );
        return;
      }
      success("설치 명령을 복사했어요.");
      setCopied(true);
    }
  }

  const sections = typeFilter
    ? SECTION_ORDER.filter((s) => s.type === typeFilter)
    : SECTION_ORDER;

  return (
    <div>
      <div className="mb-6">
        <div className="mb-1 text-xs text-muted-foreground">
          카탈로그 / <span className="font-medium text-foreground">둘러보기</span>
        </div>
        <h1 className="text-2xl font-bold tracking-tight">
          {q ? `"${q}" 검색 결과` : "카탈로그"}
        </h1>
        <p className="mt-1 text-muted-foreground">
          사내에서 공유된 Agent · Skill · MCP 도구를 발견하세요.
        </p>
      </div>

      <div className="mb-8 flex items-center gap-3">
        <span className="text-sm text-muted-foreground">타입</span>
        <Select value={typeFilter} onChange={(e) => setTypeFilter(e.target.value)}>
          <option value="">전체</option>
          {TYPE_FILTER_OPTIONS.map((t) => (
            <option key={t} value={t}>{ASSET_TYPE_META[t]?.label || t}</option>
          ))}
        </Select>
      </div>

      {q ? (
        // 검색 결과 — 단일 리스트(랭킹순)
        searchLoading ? (
          <div className="py-16 text-center text-muted-foreground">불러오는 중...</div>
        ) : !searchResults || searchResults.length === 0 ? (
          <div className="py-16 text-center text-muted-foreground">
            결과가 없어요. 다른 검색어를 시도해 보세요.
          </div>
        ) : (
          <div className="grid grid-cols-1 gap-5 md:grid-cols-2 lg:grid-cols-3">
            {searchResults.map((asset) => {
              const meta = ASSET_TYPE_META[asset.descriptor_type as DescriptorType];
              return (
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
                    </div>
                  </Card>
                </a>
              );
            })}
          </div>
        )
      ) : (
        // 둘러보기 — 타입별 섹션 페이징
        <>
          <TopDownloads />
          <div className="space-y-10">
            {sections.map((section) => (
              <CatalogSection
                key={section.type}
                type={section.type as DescriptorType}
                label={section.label}
                onInstall={openInstall}
              />
            ))}
          </div>
        </>
      )}

      <Modal
        open={Boolean(install)}
        size="lg"
        title="내 환경에 설치"
        description={install?.note}
        onClose={() => setInstall(null)}
        footer={
          <>
            {install?.command && (
              <Button size="md" onClick={copyInstall}>
                {copied ? "복사됨 ✓" : "복사"}
              </Button>
            )}
            <Button variant="outline" size="md" onClick={() => setInstall(null)}>
              닫기
            </Button>
          </>
        }
      >
        {install?.command ? (
          <pre className="overflow-x-auto whitespace-pre-wrap break-all rounded-lg bg-slate-900 p-3 text-xs text-slate-100">
            {install.command}
          </pre>
        ) : (
          <div className="rounded-lg bg-amber-50 p-3 text-sm text-amber-700">
            배포 대기 중이에요.
          </div>
        )}
      </Modal>
    </div>
  );
}
