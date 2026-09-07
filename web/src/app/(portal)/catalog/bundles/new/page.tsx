"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { ApiError, createBundle } from "@/lib/api";
import { AssetMemberPicker } from "@/components/admin/bundles/AssetMemberPicker";
import { Button } from "@/components/ui/button";
import { Icon } from "@/components/ui/icon";
import { useToast } from "@/components/ui/toast";

const SURFACES = [
  { value: "claude-code", label: "Claude Code" },
  { value: "claude-desktop", label: "Claude Desktop" },
];

export default function NewBundlePage() {
  const router = useRouter();
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [surfaces, setSurfaces] = useState<string[]>(["claude-code"]);
  const [memberIds, setMemberIds] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const { success, error: toastError } = useToast();

  function toggle(list: string[], v: string) {
    return list.includes(v) ? list.filter((x) => x !== v) : [...list, v];
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const b = await createBundle({
        name, description, member_ids: memberIds, surfaces,
      });
      success("플러그인을 만들었어요.", `${name.trim()} 상세로 이동해요.`);
      router.push(`/catalog/bundles/${encodeURIComponent(b.bundle_id)}`);
    } catch (err) {
      const message =
        err instanceof ApiError && err.status === 422
          ? err.message
          : err instanceof Error
            ? err.message
            : "만들지 못했어요";
      setError(message);
      toastError("플러그인을 만들지 못했어요.", message);
      setBusy(false);
    }
  }

  const canSubmit = name.trim().length > 0 && memberIds.length > 0 && !busy;

  return (
    <div className="mx-auto max-w-2xl">
      <Link
        href="/catalog/bundles"
        className="mb-4 inline-block text-sm text-blue-600 hover:underline"
      >
        ← 플러그인
      </Link>
      <h1 className="mb-1 text-2xl font-bold">플러그인 만들기</h1>
      <p className="mb-6 text-sm text-slate-500">
        승인된 자산을 묶어 조직에 배포를 신청할 수 있어요.
      </p>

      <form onSubmit={submit} className="space-y-6">
        <div>
          <label htmlFor="bundle-name" className="mb-1 block text-sm font-medium">
            이름
          </label>
          <input
            id="bundle-name"
            value={name}
            onChange={(e) => setName(e.target.value)}
            maxLength={64}
            placeholder="예: 프론트엔드 개발 도구"
            className="w-full rounded-lg border border-border bg-background px-3 py-2 text-sm"
          />
          <p className="mt-1 text-xs text-muted-foreground">
            소문자·하이픈으로 변환돼요. 64자 이내.
          </p>
        </div>

        <div>
          <label htmlFor="bundle-desc" className="mb-1 block text-sm font-medium">
            설명
          </label>
          <input
            id="bundle-desc"
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            placeholder="이 플러그인이 무엇을 하는지 한 줄로"
            className="w-full rounded-lg border border-border bg-background px-3 py-2 text-sm"
          />
        </div>

        <div>
          <span className="mb-1.5 block text-sm font-medium">배포 대상</span>
          <div className="flex gap-4">
            {SURFACES.map((s) => (
              <label key={s.value} className="flex cursor-pointer items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  checked={surfaces.includes(s.value)}
                  onChange={() => setSurfaces((v) => toggle(v, s.value))}
                  className="h-4 w-4 rounded accent-blue-600"
                />
                {s.label}
              </label>
            ))}
          </div>
        </div>

        <div className="space-y-4">
          <AssetMemberPicker
            type="Agent Skills"
            label="Skill"
            selected={memberIds}
            onToggle={(id) => setMemberIds((v) => toggle(v, id))}
          />
          <AssetMemberPicker
            type="MCP"
            label="MCP"
            selected={memberIds}
            onToggle={(id) => setMemberIds((v) => toggle(v, id))}
          />
        </div>

        {error && (
          <p className="rounded-lg bg-red-50 p-3 text-sm text-red-600">{error}</p>
        )}

        <div className="flex items-center gap-3">
          <Button type="submit" disabled={!canSubmit}>
            <Icon name="plus" size={14} />
            {busy ? "만드는 중…" : "만들기"}
          </Button>
          <span className="text-xs text-muted-foreground">
            자산 {memberIds.length}개 선택
          </span>
        </div>
      </form>
    </div>
  );
}
