"use client";

import { useState } from "react";
import useSWR from "swr";
import {
  listBundles, createBundle, updateBundle, deleteBundle, getBundle,
  type BundleSummary, type BundleInput,
} from "@/lib/api";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Icon } from "@/components/ui/icon";
import { useConfirm } from "@/components/ui/confirm-dialog";
import { cn } from "@/lib/ui";
import { PublishBundleButton } from "./PublishBundleButton";
import { AssetMemberPicker } from "./AssetMemberPicker";
import { HandoffModal } from "./HandoffModal";
import { useToast } from "@/components/ui/toast";

type FormState = BundleInput & { bundle_id?: string };

const EMPTY: FormState = {
  name: "", description: "", member_ids: [], surfaces: [], category: "", tags: [],
};

// 배포 서피스 — 값은 실제 surface 문자열, 라벨은 사람이 읽는 이름.
const SURFACE_OPTIONS: { value: string; label: string; hint: string }[] = [
  { value: "claude-code", label: "Claude Code", hint: "CLI · IDE" },
  { value: "claude-desktop", label: "Claude Desktop", hint: "데스크톱 앱" },
];

// 카드 stat — 타입별 연결 개수 표시. 0인 타입은 흐리게(있으면 강조).
const MEMBER_STATS: { key: "skill_count" | "mcp_count" | "agent_count"; label: string }[] = [
  { key: "skill_count", label: "Skill" },
  { key: "mcp_count", label: "MCP" },
  { key: "agent_count", label: "Agent" },
];

export function BundlesClient() {
  const { data, error: loadError, mutate, isLoading } = useSWR<BundleSummary[]>("admin-bundles", () => listBundles());
  const [form, setForm] = useState<FormState | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  // 콘솔 인계 안내 모달 대상. null이면 닫힌 상태예요.
  const [handoff, setHandoff] = useState<{ id: string; name: string } | null>(null);
  const [actionError, setActionError] = useState("");
  const { success, error: toastError } = useToast();
  const { confirm, dialog } = useConfirm();

  function startCreate() { setForm({ ...EMPTY }); setError(""); setActionError(""); }
  async function startEdit(id: string) {
    setActionError("");
    try {
      const b = await getBundle(id);
      setForm({
        bundle_id: b.bundle_id, name: b.name, description: b.description,
        member_ids: b.members.map((m) => m.record_id), surfaces: b.surfaces,
        category: b.category, tags: b.tags,
      });
      setError("");
    } catch (e) {
      setActionError(e instanceof Error ? e.message : "플러그인을 불러오지 못했어요.");
    }
  }

  function up<K extends keyof FormState>(k: K, v: FormState[K]) {
    setForm((f) => (f ? { ...f, [k]: v } : f));
  }

  function toggleSurface(value: string) {
    const current = form?.surfaces ?? [];
    up("surfaces", current.includes(value) ? current.filter((s) => s !== value) : [...current, value]);
  }

  function toggleMember(recordId: string) {
    const current = form?.member_ids ?? [];
    up("member_ids", current.includes(recordId) ? current.filter((m) => m !== recordId) : [...current, recordId]);
  }

  async function save() {
    if (!form || !form.name.trim()) { setError("이름을 입력해 주세요."); return; }
    setBusy(true); setError("");
    try {
      const body: BundleInput = {
        name: form.name, description: form.description, member_ids: form.member_ids,
        surfaces: form.surfaces, category: form.category, tags: form.tags,
      };
      const editing = Boolean(form.bundle_id);
      if (form.bundle_id) await updateBundle(form.bundle_id, body);
      else await createBundle(body);
      setForm(null);
      await mutate();
      success(editing ? "저장했어요." : "플러그인을 만들었어요.", form.name.trim());
    } catch (e) {
      const message = e instanceof Error ? e.message : "저장에 실패했어요.";
      setError(message);
      toastError("저장하지 못했어요.", message);
    } finally {
      setBusy(false);
    }
  }

  async function remove(id: string, name: string) {
    const ok = await confirm({
      title: "플러그인 삭제",
      description: `"${name}"을(를) 삭제할까요? 이 동작은 되돌릴 수 없어요.`,
      confirmLabel: "삭제",
      variant: "destructive",
    });
    if (!ok) return;
    setActionError("");
    try {
      await deleteBundle(id);
      await mutate();
      success("삭제했어요.", name);
    } catch (e) {
      const message = e instanceof Error ? e.message : "삭제에 실패했어요.";
      setActionError(message);
      toastError("삭제하지 못했어요.", message);
    }
  }

  const memberCount = form?.member_ids?.length ?? 0;

  // 폼 뷰(생성·편집) — 리스트를 감추고 폼만 보여줘요. 상단 "목록" 버튼으로 복귀.
  if (form) {
    return (
      <div>
        {dialog}
        <button
          type="button"
          onClick={() => setForm(null)}
          className="mb-4 inline-flex items-center gap-1.5 text-[13px] text-muted-foreground transition-colors hover:text-foreground"
        >
          <Icon name="back" size={14} />
          목록
        </button>

        <div className="mb-6">
          <h1 className="text-2xl font-bold tracking-tight">
            {form.bundle_id ? "플러그인 편집" : "새 플러그인"}
          </h1>
          <p className="mt-1 text-sm text-muted-foreground">
            자산을 묶어 서피스에 배포하는 플러그인을 구성해요.
          </p>
        </div>

        <Card>
          <CardHeader className="border-b border-border">
            <CardTitle className="text-base">기본 구성</CardTitle>
          </CardHeader>
          <CardContent className="space-y-8 pt-5">
            {/* 섹션 1 — 기본 정보 */}
            <FormSection title="기본 정보" desc="플러그인의 이름과 설명, 분류를 입력해요.">
              <div className="space-y-3">
                <Field label="플러그인 이름" htmlFor="bundle-name" required>
                  <Input
                    id="bundle-name"
                    placeholder="예: 프론트엔드 리뷰 킷"
                    value={form.name}
                    onChange={(e) => up("name", e.target.value)}
                  />
                </Field>
                <Field label="설명" htmlFor="bundle-desc">
                  <textarea
                    id="bundle-desc"
                    rows={2}
                    placeholder="이 플러그인이 무엇을 하는지 한두 줄로 설명해요."
                    value={form.description ?? ""}
                    onChange={(e) => up("description", e.target.value)}
                    className="w-full rounded-lg border border-input bg-card px-3.5 py-2 text-sm text-foreground placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-1"
                  />
                </Field>
                <div className="grid gap-3 sm:grid-cols-2">
                  <Field label="카테고리" htmlFor="bundle-category">
                    <Input
                      id="bundle-category"
                      placeholder="예: 개발 생산성"
                      value={form.category ?? ""}
                      onChange={(e) => up("category", e.target.value)}
                    />
                  </Field>
                  <Field label="태그" htmlFor="bundle-tags" hint="쉼표로 구분">
                    <Input
                      id="bundle-tags"
                      placeholder="review, frontend"
                      value={(form.tags ?? []).join(", ")}
                      onChange={(e) => up("tags", e.target.value.split(",").map((s) => s.trim()).filter(Boolean))}
                    />
                  </Field>
                </div>
              </div>
            </FormSection>

            {/* 섹션 2 — 배포 서피스 */}
            <FormSection title="배포 서피스" desc="이 플러그인을 어떤 클라이언트에 등록할지 선택해요.">
              <div className="grid gap-2.5 sm:grid-cols-2">
                {SURFACE_OPTIONS.map((s) => {
                  const checked = (form.surfaces ?? []).includes(s.value);
                  return (
                    <label
                      key={s.value}
                      className={cn(
                        "flex cursor-pointer items-center gap-3 rounded-lg border px-3.5 py-3 transition-colors",
                        checked
                          ? "border-blue-500 bg-blue-50/60"
                          : "border-input bg-card hover:bg-accent",
                      )}
                    >
                      <input
                        type="checkbox"
                        checked={checked}
                        onChange={() => toggleSurface(s.value)}
                        className="h-4 w-4 cursor-pointer rounded border-input accent-blue-600 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-1"
                      />
                      <span className="min-w-0">
                        <span className="block text-sm font-medium text-foreground">{s.label}</span>
                        <span className="block text-xs text-muted-foreground">{s.hint}</span>
                      </span>
                    </label>
                  );
                })}
              </div>
            </FormSection>

            {/* 섹션 3 — 멤버 자산 */}
            <FormSection
              title="멤버 자산"
              desc="플러그인에 포함할 자산을 선택해요. 승인된(APPROVED) 자산만 보여요."
            >
              <div className="mb-3 text-xs text-muted-foreground">
                선택된 자산 <span className="font-medium text-foreground">{memberCount}개</span>
              </div>
              <div className="grid gap-5 lg:grid-cols-2">
                <AssetMemberPicker
                  type="Agent Skills"
                  label="Skill"
                  selected={form.member_ids ?? []}
                  onToggle={toggleMember}
                />
                <AssetMemberPicker
                  type="MCP"
                  label="MCP"
                  selected={form.member_ids ?? []}
                  onToggle={toggleMember}
                />
              </div>
            </FormSection>

            {error && <div className="text-sm text-red-600">{error}</div>}

            <div className="flex gap-2 border-t border-border pt-5">
              <Button onClick={save} disabled={busy}>
                {busy ? "저장 중…" : "저장"}
              </Button>
              <Button variant="outline" onClick={() => setForm(null)} disabled={busy}>
                취소
              </Button>
            </div>
          </CardContent>
        </Card>
      </div>
    );
  }

  // 리스트 뷰 — 폼이 없을 때만 카드 목록을 보여줘요.
  return (
    <div>
      {dialog}
      {handoff && (
        <HandoffModal
          bundleId={handoff.id}
          bundleName={handoff.name}
          onClose={() => setHandoff(null)}
        />
      )}
      <div className="mb-6 flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">플러그인 관리</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            자산을 묶어 서피스에 배포하는 플러그인을 만들고 관리해요.
          </p>
        </div>
        <Button onClick={startCreate}>
          <Icon name="plus" size={16} />
          새 플러그인
        </Button>
      </div>

      {actionError && (
        <div className="mb-4 rounded-lg border border-red-200 bg-red-50 p-3 text-sm text-red-700">
          {actionError}
        </div>
      )}

      {isLoading && <BundlesSkeleton />}

      {loadError && (
        <Card className="p-6 text-sm text-red-700">
          플러그인 목록을 불러오지 못했어요. API 서버(:9100)가 떠 있는지 확인해 주세요.
        </Card>
      )}

      {data && data.length === 0 && (
        <div className="rounded-xl border border-dashed border-border p-12 text-center">
          <p className="text-sm font-medium text-foreground">아직 플러그인이 없어요</p>
          <p className="mt-1 text-sm text-muted-foreground">
            자산을 묶어 첫 플러그인을 만들어 보세요.
          </p>
          <Button className="mt-4" onClick={startCreate}>
            <Icon name="plus" size={16} />
            새 플러그인
          </Button>
        </div>
      )}

      {data && data.length > 0 && (
        <div className="grid grid-cols-1 gap-5 md:grid-cols-2 xl:grid-cols-3">
          {data.map((b) => (
            <BundleCard
              key={b.bundle_id}
              bundle={b}
              onEdit={() => startEdit(b.bundle_id)}
              onDelete={() => remove(b.bundle_id, b.name)}
              onPublished={() => mutate()}
              onHandoff={() => setHandoff({ id: b.bundle_id, name: b.name })}
            />
          ))}
        </div>
      )}
    </div>
  );
}

function BundleCard({
  bundle,
  onEdit,
  onDelete,
  onPublished,
  onHandoff,
}: {
  bundle: BundleSummary;
  onEdit: () => void;
  onDelete: () => void;
  onPublished: () => void;
  onHandoff: () => void;
}) {
  const stats = MEMBER_STATS.filter((s) => s.key !== "agent_count" || bundle[s.key] > 0);
  return (
    <Card className="flex h-full flex-col transition-all hover:border-slate-300 hover:shadow-md">
      <CardContent className="flex flex-1 flex-col gap-3 px-5 pb-5 pt-6">
        <div>
          <button
            type="button"
            onClick={onEdit}
            className="rounded text-left font-semibold text-card-foreground transition-colors hover:text-blue-600 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
          >
            {bundle.name}
          </button>
          <p className="mt-1 line-clamp-2 text-sm text-muted-foreground">
            {bundle.description?.trim() || "설명이 없어요"}
          </p>
        </div>

        {/* 등록되는 서피스 */}
        <div className="flex flex-wrap items-center gap-1.5">
          {bundle.surfaces.length > 0 ? (
            bundle.surfaces.map((s) => (
              <Badge key={s} variant="type" className="bg-blue-50 text-blue-700">
                {SURFACE_OPTIONS.find((o) => o.value === s)?.label ?? s}
              </Badge>
            ))
          ) : (
            <span className="text-xs text-muted-foreground/70">서피스 없음</span>
          )}
        </div>

        {/* skill/mcp/agent 연결 개수 */}
        <div className="mt-auto flex flex-wrap gap-x-3 gap-y-1 text-xs">
          {stats.map((s) => {
            const n = bundle[s.key];
            return (
              <span
                key={s.key}
                className={cn(
                  "tabular-nums",
                  n > 0 ? "text-foreground" : "text-muted-foreground/50",
                )}
              >
                {s.label} <span className="font-semibold">{n}</span>
              </span>
            );
          })}
        </div>
      </CardContent>

      <div className="flex items-center gap-2 border-t border-border px-5 py-3">
        <PublishBundleButton bundleId={bundle.bundle_id} onDone={onPublished} />
        <div className="ml-auto flex items-center gap-1.5">
          <Button variant="ghost" size="sm" onClick={onHandoff}>등록 안내</Button>
          <Button variant="outline" size="sm" onClick={onEdit}>편집</Button>
          <Button
            variant="ghost"
            size="sm"
            onClick={onDelete}
            className="text-red-600 hover:bg-red-50 hover:text-red-700"
          >
            삭제
          </Button>
        </div>
      </div>
    </Card>
  );
}

function FormSection({
  title,
  desc,
  children,
}: {
  title: string;
  desc?: string;
  children: React.ReactNode;
}) {
  return (
    <section>
      <div className="mb-3">
        <h4 className="text-sm font-semibold text-foreground">{title}</h4>
        {desc && <p className="mt-0.5 text-xs text-muted-foreground">{desc}</p>}
      </div>
      {children}
    </section>
  );
}

function Field({
  label,
  htmlFor,
  hint,
  required,
  children,
}: {
  label: string;
  htmlFor: string;
  hint?: string;
  required?: boolean;
  children: React.ReactNode;
}) {
  return (
    <div>
      <label htmlFor={htmlFor} className="mb-1 flex items-center gap-1.5 text-xs font-medium text-foreground">
        {label}
        {required && <span className="text-red-500">*</span>}
        {hint && <span className="font-normal text-muted-foreground">· {hint}</span>}
      </label>
      {children}
    </div>
  );
}

function BundlesSkeleton() {
  return (
    <div className="grid grid-cols-1 gap-5 md:grid-cols-2 xl:grid-cols-3">
      {Array.from({ length: 3 }).map((_, i) => (
        <div key={i} className="h-44 animate-pulse rounded-xl bg-muted/50" />
      ))}
    </div>
  );
}
