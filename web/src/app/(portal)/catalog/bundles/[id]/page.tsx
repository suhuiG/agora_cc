"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import useSWR from "swr";
import { getBundle, type BundleDetail } from "@/lib/api";
import { RequestDeployButton } from "@/components/bundles/RequestDeployButton";

export default function BundleDetailPage() {
  const params = useParams();
  const id = String(params.id);
  const { data, error, isLoading, mutate } = useSWR<BundleDetail>(
    id ? `bundle-${id}` : null,
    () => getBundle(id),
  );

  return (
    <div className="max-w-3xl mx-auto">
      <Link href="/catalog/bundles" className="text-sm text-blue-600 hover:underline mb-4 inline-block">
        ← 플러그인
      </Link>

      {error && <div className="text-red-600 text-sm bg-red-50 p-3 rounded-lg">플러그인을 불러오지 못했어요.</div>}
      {isLoading && <div className="text-slate-400 text-sm">불러오는 중…</div>}

      {data && (
        <>
          <div className="mb-1 flex items-start justify-between gap-4">
            <h1 className="text-2xl font-bold">{data.name}</h1>
            <RequestDeployButton
              bundleId={id}
              status={data.request_status}
              onDone={() => mutate()}
            />
          </div>
          {data.description && <p className="text-sm text-slate-500 mb-4">{data.description}</p>}
          <div className="flex flex-wrap gap-1.5 mb-6">
            {data.surfaces.map((s) => (
              <span key={s} className="text-xs text-blue-700 bg-blue-50 px-2 py-0.5 rounded">{s}</span>
            ))}
            {data.tags.map((t) => (
              <span key={t} className="text-xs text-slate-600 bg-slate-100 px-2 py-0.5 rounded">{t}</span>
            ))}
          </div>

          <h2 className="text-sm font-semibold text-slate-700 mb-2">
            포함 자산 ({data.members.filter((m) => m.available).length}/{data.members.length} 이용 가능)
          </h2>
          <div className="border border-slate-200 rounded-lg divide-y divide-slate-100">
            {data.members.map((m) => (
              <div key={m.record_id} className="flex items-center gap-3 px-4 py-3">
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2">
                    <span className={`font-medium truncate ${m.available ? "text-slate-800" : "text-slate-400"}`}>
                      {m.name}
                    </span>
                    {m.type && (
                      <span className="text-xs bg-slate-100 text-slate-600 px-2 py-0.5 rounded shrink-0">{m.type}</span>
                    )}
                  </div>
                </div>
                {m.available ? (
                  <Link
                    href={`/catalog/assets/${encodeURIComponent(m.record_id)}`}
                    className="text-sm text-blue-600 hover:underline shrink-0"
                  >
                    상세
                  </Link>
                ) : (
                  <span className="text-xs text-slate-400 shrink-0">이용 불가 ({m.status})</span>
                )}
              </div>
            ))}
          </div>
        </>
      )}
    </div>
  );
}
