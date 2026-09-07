"use client";

import { useState } from "react";
import useSWR from "swr";

import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import {
  getGovCategories,
  putGovCategories,
  type GovCategories,
} from "@/lib/api";

export function CategoriesClient() {
  const { data, error, isLoading, mutate } = useSWR<GovCategories>(
    "gov/categories",
    getGovCategories,
  );
  const [draft, setDraft] = useState<string[] | null>(null);
  const [input, setInput] = useState("");
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);

  const categories = draft ?? data?.items ?? [];
  const dirty = data
    ? JSON.stringify(categories) !== JSON.stringify(data.items)
    : false;

  function add(value: string) {
    const trimmed = value.trim();
    if (!trimmed || categories.includes(trimmed)) return;
    setDraft([...categories, trimmed]);
    setInput("");
  }

  async function save() {
    setSaving(true);
    setSaveError(null);
    try {
      await putGovCategories(categories);
      await mutate();
      setDraft(null);
    } catch {
      setSaveError("저장 중 오류가 발생했어요.");
    } finally {
      setSaving(false);
    }
  }

  if (isLoading) {
    return (
      <Shell>
        <div className="h-40 animate-pulse rounded-lg bg-muted/50" />
      </Shell>
    );
  }
  if (error || !data) {
    return (
      <Shell>
        <Card className="rounded-lg p-6 text-sm text-red-700">
          카테고리를 불러오지 못했어요.
        </Card>
      </Shell>
    );
  }

  return (
    <Shell>
      <div className="space-y-5">
        <Card className="rounded-lg p-5">
          <div className="mb-4 flex gap-2">
            <Input
              value={input}
              onChange={(event) => setInput(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter") {
                  event.preventDefault();
                  add(input);
                }
              }}
              placeholder="카테고리 이름"
              className="max-w-xs"
            />
            <Button
              size="sm"
              variant="outline"
              onClick={() => add(input)}
              disabled={!input.trim()}
            >
              추가
            </Button>
          </div>

          {categories.length === 0 ? (
            <div className="rounded-lg border border-dashed border-border py-8 text-center text-xs text-muted-foreground">
              등록된 카테고리가 없어요
            </div>
          ) : (
            <ul className="divide-y divide-border rounded-lg border border-border">
              {categories.map((name, index) => (
                <li
                  key={name}
                  className="flex items-center justify-between gap-3 px-4 py-2.5 text-sm"
                >
                  <span className="min-w-0 truncate">{name}</span>
                  <div className="flex shrink-0 gap-1">
                    <IconButton
                      label={`${name} 위로`}
                      symbol="↑"
                      disabled={index === 0}
                      onClick={() => {
                        setDraft(swap(categories, index, index - 1));
                      }}
                    />
                    <IconButton
                      label={`${name} 아래로`}
                      symbol="↓"
                      disabled={index === categories.length - 1}
                      onClick={() => {
                        setDraft(swap(categories, index, index + 1));
                      }}
                    />
                    <IconButton
                      label={`${name} 삭제`}
                      symbol="×"
                      danger
                      onClick={() => {
                        setDraft(categories.filter((item) => item !== name));
                      }}
                    />
                  </div>
                </li>
              ))}
            </ul>
          )}

          {saveError && (
            <div className="mt-3 text-sm text-red-700">{saveError}</div>
          )}
          <div className="mt-4">
            <Button size="sm" onClick={save} disabled={!dirty || saving}>
              {saving ? "저장 중..." : dirty ? "저장" : "저장됨"}
            </Button>
          </div>
        </Card>

        <Card className="rounded-lg p-5">
          <div className="mb-3 text-sm font-semibold">
            사용 중인 미등록 값
          </div>
          {data.in_use_unlisted.length === 0 ? (
            <div className="text-xs text-muted-foreground">
              미등록 값이 없어요
            </div>
          ) : (
            <div className="flex flex-wrap gap-2">
              {data.in_use_unlisted.map((name) => (
                <button
                  key={name}
                  type="button"
                  onClick={() => add(name)}
                  className="rounded-full border border-border px-2.5 py-1 text-xs hover:bg-muted"
                  title="목록에 추가"
                >
                  {name} <span aria-hidden="true">+</span>
                </button>
              ))}
            </div>
          )}
        </Card>
      </div>
    </Shell>
  );
}

function Shell({ children }: { children: React.ReactNode }) {
  return (
    <div>
      <div className="mb-6">
        <h1 className="text-2xl font-bold">카테고리</h1>
      </div>
      {children}
    </div>
  );
}

function IconButton({
  label,
  symbol,
  disabled = false,
  danger = false,
  onClick,
}: {
  label: string;
  symbol: string;
  disabled?: boolean;
  danger?: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      aria-label={label}
      title={label}
      disabled={disabled}
      onClick={onClick}
      className={`grid size-8 place-items-center rounded-md text-sm disabled:opacity-30 ${
        danger
          ? "text-red-600 hover:bg-red-50"
          : "text-muted-foreground hover:bg-muted"
      }`}
    >
      <span aria-hidden="true">{symbol}</span>
    </button>
  );
}

function swap(items: string[], first: number, second: number): string[] {
  const next = [...items];
  [next[first], next[second]] = [next[second], next[first]];
  return next;
}
