"use client";

import Link from "next/link";
import useSWR from "swr";
import { listBundles, type BundleSummary } from "@/lib/api";

export default function BundlesPage() {
  const { data, error, isLoading } = useSWR<BundleSummary[]>("bundles", () => listBundles());

  return (
    <div className="max-w-4xl mx-auto">
      <div className="flex items-center justify-between gap-4 mb-1">
        <h1 className="text-2xl font-bold">플러그인</h1>
        <Link
          href="/catalog/bundles/new"
          className="rounded-lg bg-blue-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-blue-700"
        >
          + 플러그인 만들기
        </Link>
      </div>
      <p className="text-sm text-slate-400 mb-6">
        승인된 자산을 묶은 플러그인이에요. 직접 만들어 조직 배포를 신청할 수도 있어요.
      </p>

      {error && (
        <div className="text-red-600 text-sm bg-red-50 p-3 rounded-lg">
          플러그인 목록을 불러오지 못했어요.
        </div>
      )}
      {isLoading && <div className="text-slate-400 text-sm">불러오는 중…</div>}
      {data && data.length === 0 && (
        <div className="text-slate-500 text-sm border border-dashed border-slate-200 rounded-lg p-8 text-center">
          아직 플러그인이 없어요.
        </div>
      )}

      {data && data.length > 0 && (
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
          {data.map((b) => (
            <Link
              key={b.bundle_id}
              href={`/catalog/bundles/${encodeURIComponent(b.bundle_id)}`}
              className="border border-slate-200 rounded-lg p-4 bg-white hover:border-slate-400 transition-colors"
            >
              <div className="flex items-center gap-2 mb-1">
                <span className="font-semibold text-slate-800">{b.name}</span>
                <span className="text-xs bg-slate-100 text-slate-600 px-2 py-0.5 rounded">
                  자산 {b.member_count}개
                </span>
              </div>
              {b.description && (
                <p className="text-sm text-slate-500 line-clamp-2">{b.description}</p>
              )}
              {b.surfaces.length > 0 && (
                <div className="flex flex-wrap gap-1.5 mt-2">
                  {b.surfaces.map((s) => (
                    <span key={s} className="text-xs text-blue-700 bg-blue-50 px-2 py-0.5 rounded">{s}</span>
                  ))}
                </div>
              )}
            </Link>
          ))}
        </div>
      )}
    </div>
  );
}
