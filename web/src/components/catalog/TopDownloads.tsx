"use client";

import { useState } from "react";
import useSWR from "swr";
import { getTopDownloads, type DescriptorType, type TopDownloadGroup } from "@/lib/api";
import { ASSET_TYPE_META } from "@/lib/assetTypes";
import { principalPresentation } from "@/lib/principal";
import { Card } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";

/**
 * 인기 자산 — 자산 타입마다 카드 하나, 카드 안에 그 타입의 톱5.
 *
 * 순위는 서버가 매 호출마다 실집계해요(1시간 캐시에 갇히지 않음). 그래서 화면을
 * 다시 열면 최신 순위가 나오고, 새로고침 버튼은 그 자리에서 재정렬해요.
 */
export function TopDownloads() {
  const [refreshing, setRefreshing] = useState(false);
  const { data, error, mutate } = useSWR(
    "/api/catalog/top-downloads",
    () => getTopDownloads(),
    // 화면이 열릴 때마다 최신 순위를 받아요. 전역 dedupingInterval(30초)이 남아
    // 있으면 방금 오른 다운로드가 안 보이므로 이 키만 중복제거를 끕니다.
    { revalidateOnMount: true, revalidateIfStale: true, dedupingInterval: 0 },
  );

  async function refresh() {
    setRefreshing(true);
    try {
      // force=1 로 서버 재집계를 요청하고 그 결과로 캐시를 갈아끼워요.
      await mutate(getTopDownloads(true), { revalidate: false });
    } catch {
      /* 실패해도 기존 순위는 그대로 남겨요 */
    } finally {
      setRefreshing(false);
    }
  }

  // 조회 실패거나 아직 데이터가 없으면 렌더하지 않아요 — 카탈로그 본문은 정상이에요.
  if (error || !data) return null;

  // 카드로 보여줄 타입만 남겨요(ASSET_TYPE_META에 없는 타입은 배지를 못 만들어요).
  const groups = data.groups.filter((g) => ASSET_TYPE_META[g.descriptor_type]);
  if (groups.length === 0) return null;

  const timeLabel = new Date(data.computed_at).toLocaleTimeString("ko-KR", {
    timeZone: "Asia/Seoul",
    hour: "2-digit",
    minute: "2-digit",
  });

  return (
    <section className="mb-10">
      <div className="mb-4 flex items-center gap-2 border-b border-border pb-2">
        <span className="text-base font-semibold">인기 자산</span>
        <Button
          variant="ghost"
          size="sm"
          onClick={refresh}
          disabled={refreshing}
          aria-label="인기 자산 순위 새로고침"
          title="다운로드 수로 다시 정렬해요"
        >
          <span aria-hidden className={refreshing ? "animate-spin" : undefined}>
            ↻
          </span>
          {refreshing ? "갱신 중" : "새로고침"}
        </Button>
        <span className="ml-auto text-sm text-muted-foreground">
          {timeLabel} 기준 · 다운로드 순
        </span>
      </div>

      <div className="grid grid-cols-1 gap-5 md:grid-cols-2 lg:grid-cols-3">
        {groups.map((group) => (
          <TypeCard key={group.descriptor_type} group={group} />
        ))}
      </div>
    </section>
  );
}

/** 자산 타입 하나의 카드 — 안에 그 타입의 톱5 순위 목록. */
function TypeCard({ group }: { group: TopDownloadGroup }) {
  const meta = ASSET_TYPE_META[group.descriptor_type as DescriptorType];

  return (
    <Card className="flex h-full flex-col p-5">
      <div className="mb-3 flex items-center justify-between border-b border-border pb-2">
        <Badge variant="type" className={meta.pill}>
          {meta.label}
        </Badge>
        {group.items.length > 0 && (
          <span className="text-xs text-muted-foreground">
            상위 {group.items.length}개
          </span>
        )}
      </div>

      {group.items.length === 0 ? (
        <p className="py-4 text-sm text-muted-foreground">아직 다운로드가 없어요.</p>
      ) : (
        <ol className="flex flex-col gap-1">
          {group.items.map((entry, idx) => (
            <li key={entry.record_id}>
              <a
                href={`/catalog/assets/${entry.record_id}`}
                className="flex items-center gap-2 rounded-lg px-2 py-1.5 transition-colors hover:bg-accent"
              >
                <span className="w-4 shrink-0 text-sm font-semibold text-muted-foreground">
                  {idx + 1}
                </span>
                <span className="min-w-0 flex-1 truncate text-sm font-medium text-card-foreground">
                  {entry.name}
                </span>
                {/* 오른쪽은 등록자명이에요. 순위 자체가 다운로드 순이라 숫자를
                    반복해 보여주기보다 "누가 올렸는지"가 정보량이 커요. */}
                <span
                  className="max-w-[45%] shrink-0 truncate text-xs text-muted-foreground"
                  title={entry.owner_email || entry.owner_user || undefined}
                >
                  {principalPresentation(
                    entry.owner_email || entry.owner_user,
                  ).label}
                </span>
              </a>
            </li>
          ))}
        </ol>
      )}
    </Card>
  );
}
