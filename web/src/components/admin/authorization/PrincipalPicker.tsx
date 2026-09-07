"use client";

import { useState } from "react";
import { listCognitoUsers, type CognitoUser } from "@/lib/api";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Icon } from "@/components/ui/icon";
import { InlineError, errorMessage } from "@/components/admin/access/shared";

// 부여 대상(사람) 선택 — `ToolAuthorizationClient.tsx` 에서 꺼냈어요.
//
// 두 화면이 같은 선택기를 써요: ④축 승인 화면(`/admin/tool-authorization`)과 ⑦축 도구별 호출
// 주체 화면(`/admin/tool-access`). 복사해 두면 한쪽만 고쳐지고, 「누구에게 권한을 주는가」는
// 두 화면에서 같은 방식으로 확정돼야 해요.

export function PrincipalPicker({
  /** 입력 id 를 유일하게 만드는 접두어. 같은 페이지에 여러 개가 뜰 수 있어요. */
  rowKey: key,
  label,
  mustPick,
  isDefault,
  onPick,
  onReset,
  /** 기본값이 없을 때의 안내 문구. 화면마다 이유가 달라요. */
  emptyHint = "email 로 검색해 부여 대상을 골라 주세요.",
  /** 기본값이 없는 이유. `/admin/tool-access` 처럼 원래 기본값이 없는 화면은 비워요. */
  noDefaultHint = "신청자가 사람이 아니어서 기본값이 없어요 — 직접 골라 주세요.",
}: {
  rowKey: string;
  label: string;
  mustPick: boolean;
  isDefault: boolean;
  onPick: (user: CognitoUser) => void;
  /** 없으면 되돌릴 기본값이 없다는 뜻이에요 — 신청자가 사람이 아닌 경우예요. */
  onReset?: () => void;
  emptyHint?: string;
  noDefaultHint?: string;
}) {
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<CognitoUser[]>([]);
  const [searching, setSearching] = useState(false);
  const [searchError, setSearchError] = useState("");

  // email 로 Cognito sub 를 확정해요 — admin 은 sub 를 알 수 없어요. 정확 일치를 자동 선택
  // 하지 않고 후보를 보여줘요: 잘못된 사람에게 쓰기 권한이 가면 되돌리기가 비싸요.
  async function search(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const value = query.trim();
    if (!value) {
      setSearchError("email 을 입력해 주세요.");
      return;
    }
    setSearching(true);
    setSearchError("");
    try {
      const page = await listCognitoUsers({ query: value });
      setResults(page.items);
      if (page.items.length === 0) setSearchError("해당 email 의 사용자가 없어요.");
    } catch (caught) {
      setSearchError(errorMessage(caught, "사용자를 조회하지 못했어요."));
    } finally {
      setSearching(false);
    }
  }

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-2 text-[12.5px]">
        <span className="font-medium">부여 대상</span>
        <Badge variant="outline" className="max-w-full break-all">
          {label}
        </Badge>
        {isDefault ? (
          <span className="text-[11px] text-muted-foreground">신청자 본인</span>
        ) : onReset ? (
          <Button type="button" size="sm" variant="outline" onClick={onReset}>
            신청자로 되돌리기
          </Button>
        ) : noDefaultHint ? (
          <span className="text-[11px] text-amber-800">{noDefaultHint}</span>
        ) : null}
      </div>
      <form onSubmit={search} className="flex gap-2">
        <label htmlFor={`grant-email-${key}`} className="sr-only">
          부여 대상 email 검색
        </label>
        <Input
          id={`grant-email-${key}`}
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder="user@example.com"
        />
        <Button type="submit" variant="outline" disabled={searching} className="shrink-0">
          <Icon name="search" size={14} />
          {searching ? "검색 중" : "검색"}
        </Button>
      </form>
      {mustPick && !searchError && results.length === 0 && (
        <p className="text-[11.5px] text-amber-800">{emptyHint}</p>
      )}
      {searchError && <InlineError message={searchError} />}
      {results.length > 0 && (
        <div className="max-h-40 divide-y divide-border overflow-auto rounded-lg border border-border">
          {results.map((user) => (
            <button
              key={user.sub}
              type="button"
              onClick={() => {
                onPick(user);
                setResults([]);
                setQuery("");
              }}
              className="flex w-full min-w-0 items-center justify-between gap-3 px-3 py-2 text-left hover:bg-accent"
            >
              <span className="min-w-0">
                <span className="block truncate text-[12.5px] font-medium">
                  {user.name || user.email}
                </span>
                <span className="block truncate text-[11px] text-muted-foreground">
                  {user.email}
                </span>
              </span>
              <span className="shrink-0 text-[11px] text-primary">선택</span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
