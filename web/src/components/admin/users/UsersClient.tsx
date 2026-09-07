"use client";

import { useState } from "react";
import useSWR from "swr";

import { BulkUploadDialog } from "./BulkUploadDialog";
import { InviteUserDialog } from "./InviteUserDialog";
import { OffboardDialog } from "./OffboardDialog";
import { UserGrantsPanel } from "./UserGrantsPanel";
import { Button } from "@/components/ui/button";
import { Icon } from "@/components/ui/icon";
import { Input, Select } from "@/components/ui/input";
import {
  listCognitoUsers,
  updateCognitoUser,
  type CognitoUser,
  type CognitoUserPage,
} from "@/lib/api";

export function UsersClient() {
  const [searchDraft, setSearchDraft] = useState("");
  const [query, setQuery] = useState("");
  const [group, setGroup] = useState("");
  const [cursors, setCursors] = useState([""]);
  const [selected, setSelected] = useState<CognitoUser | null>(null);
  const [inviteOpen, setInviteOpen] = useState(false);
  const [bulkOpen, setBulkOpen] = useState(false);
  const [offboardUser, setOffboardUser] = useState<CognitoUser | null>(null);
  const [notice, setNotice] = useState("");
  const cursor = cursors.at(-1) ?? "";

  const { data, error, isLoading, mutate } = useSWR<CognitoUserPage>(
    ["admin/identity/users", query, group, cursor],
    () => listCognitoUsers({ query, group, page: cursor }),
  );

  function resetPagination() {
    setCursors([""]);
    setSelected(null);
  }

  function submitSearch(event: React.FormEvent) {
    event.preventDefault();
    setQuery(searchDraft.trim());
    resetPagination();
  }

  return (
    <div className="min-w-0">
      <div className="mb-5 flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold">사용자 관리</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            Cognito 사용자와 플랫폼 그룹을 관리해요.
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <Button variant="outline" size="sm" onClick={() => setBulkOpen(true)}>
            <Icon name="upload" size={14} />
            CSV 등록
          </Button>
          <Button size="sm" onClick={() => setInviteOpen(true)}>
            <Icon name="plus" size={14} />
            사용자 초대
          </Button>
        </div>
      </div>

      <div className="mb-4 flex flex-wrap items-center gap-2">
        <form onSubmit={submitSearch} className="flex min-w-0 flex-1 gap-2 sm:max-w-md">
          <label htmlFor="user-search" className="sr-only">
            사용자 검색
          </label>
          <Input
            id="user-search"
            value={searchDraft}
            onChange={(event) => setSearchDraft(event.target.value)}
            placeholder="이메일 앞부분 검색"
          />
          <Button type="submit" variant="outline" aria-label="검색" title="검색">
            <Icon name="search" size={16} />
          </Button>
        </form>
        <label htmlFor="user-group-filter" className="sr-only">
          그룹 필터
        </label>
        <Select
          id="user-group-filter"
          value={group}
          onChange={(event) => {
            setGroup(event.target.value);
            resetPagination();
          }}
          className="w-full sm:w-40"
        >
          <option value="">모든 그룹</option>
          <option value="user">user</option>
          <option value="admin">admin</option>
        </Select>
      </div>

      {notice && (
        <div className="mb-4 rounded-md border border-emerald-200 bg-emerald-50 px-3 py-2 text-sm text-emerald-800">
          {notice}
        </div>
      )}

      <div className="grid min-w-0 gap-5 xl:grid-cols-[minmax(0,1fr)_320px]">
        <section className="min-w-0" aria-label="사용자 목록">
          {isLoading ? (
            <LoadingRows />
          ) : error ? (
            <Message tone="error">사용자를 불러오지 못했어요.</Message>
          ) : data?.items.length === 0 ? (
            <Message>조건에 맞는 사용자가 없어요.</Message>
          ) : (
            <div className="overflow-x-auto rounded-lg border border-border">
              <table className="w-full min-w-[760px] text-left text-sm">
                <thead className="bg-muted/70 text-xs text-muted-foreground">
                  <tr>
                    <th className="w-12 px-3 py-2.5">
                      <span className="sr-only">선택</span>
                    </th>
                    <th className="px-3 py-2.5 font-medium">사용자</th>
                    <th className="px-3 py-2.5 font-medium">부서</th>
                    <th className="px-3 py-2.5 font-medium">그룹</th>
                    <th className="px-3 py-2.5 font-medium">상태</th>
                    <th className="px-3 py-2.5 font-medium">MFA</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-border">
                  {data?.items.map((user) => (
                    <tr
                      key={user.sub}
                      className={`cursor-pointer hover:bg-muted/40 ${
                        selected?.sub === user.sub ? "bg-primary/5" : ""
                      }`}
                      onClick={() => setSelected(user)}
                    >
                      <td className="px-3 py-3">
                        <input
                          type="radio"
                          name="selected-user"
                          checked={selected?.sub === user.sub}
                          onChange={() => setSelected(user)}
                          aria-label={`${user.name || user.email} 선택`}
                          className="size-4 accent-primary"
                        />
                      </td>
                      <td className="max-w-72 px-3 py-3">
                        <div className="truncate font-medium">{user.name || "-"}</div>
                        <div className="truncate text-xs text-muted-foreground">
                          {user.email}
                        </div>
                      </td>
                      <td className="max-w-44 truncate px-3 py-3">
                        {user.team || "-"}
                      </td>
                      <td className="px-3 py-3">
                        <div className="flex flex-wrap gap-1">
                          {user.groups.map((item) => (
                            <span
                              key={item}
                              className="rounded-md bg-slate-100 px-2 py-0.5 text-xs text-slate-700"
                            >
                              {item}
                            </span>
                          ))}
                        </div>
                      </td>
                      <td className="px-3 py-3">
                        <Status value={user.status} />
                      </td>
                      <td className="px-3 py-3 text-xs">
                        {user.mfa_enabled ? "설정" : "미설정"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          <div className="mt-3 flex justify-end gap-2">
            <Button
              variant="outline"
              size="sm"
              disabled={cursors.length === 1 || isLoading}
              onClick={() => {
                setCursors((current) => current.slice(0, -1));
                setSelected(null);
              }}
            >
              이전
            </Button>
            <Button
              variant="outline"
              size="sm"
              disabled={!data?.next_page || isLoading}
              onClick={() => {
                if (!data?.next_page) return;
                setCursors((current) => [...current, data.next_page ?? ""]);
                setSelected(null);
              }}
            >
              다음
            </Button>
          </div>
        </section>

        <aside className="min-w-0 rounded-lg border border-border p-4">
          {selected ? (
            <UserEditor
              key={`${selected.sub}-${selected.email}-${selected.name}-${selected.team}-${selected.groups.join(",")}`}
              user={selected}
              onSaved={async (user) => {
                setSelected(user);
                setNotice("사용자 정보가 저장됐어요.");
                await mutate();
              }}
              onOffboard={() => setOffboardUser(selected)}
            />
          ) : (
            <div className="py-10 text-center text-sm text-muted-foreground">
              사용자를 선택하면 속성과 그룹을 수정할 수 있어요.
            </div>
          )}
        </aside>
      </div>

      {/* 옛 「사용자 권한」(`/admin/grants`) 화면의 ⑦ grant 조회·회수를 이 화면이 흡수했어요
          (제품 오너 결정, 2026-09-06). nav 에서 그 메뉴를 뺀 근거가 이 패널이라서,
          `web/src/lib/nav.test.ts` 가 이 파일이 실제로 그리는지 소스 텍스트로 단정해요 —
          지우면 그 테스트가 빨개져요. */}
      {selected && <UserGrantsPanel user={selected} />}

      {inviteOpen && (
        <InviteUserDialog
          open
          onClose={() => setInviteOpen(false)}
          onCreated={async (user) => {
            setNotice(`${user.email} 사용자를 초대했어요.`);
            resetPagination();
            await mutate();
          }}
        />
      )}
      {bulkOpen && (
        <BulkUploadDialog
          open
          onClose={() => setBulkOpen(false)}
          onCommitted={async () => {
            setNotice("CSV 일괄 등록을 처리했어요.");
            resetPagination();
            await mutate();
          }}
        />
      )}
      {offboardUser && (
        <OffboardDialog
          user={offboardUser}
          onClose={() => setOffboardUser(null)}
          onDisabled={async (revoked) => {
            setNotice(`계정을 비활성화하고 grant ${revoked}건을 회수했어요.`);
            setSelected(null);
            await mutate();
          }}
        />
      )}
    </div>
  );
}

function UserEditor({
  user,
  onSaved,
  onOffboard,
}: {
  user: CognitoUser;
  onSaved: (user: CognitoUser) => void;
  onOffboard: () => void;
}) {
  const [email, setEmail] = useState(user.email);
  const [name, setName] = useState(user.name);
  const [team, setTeam] = useState(user.team);
  const [groups, setGroups] = useState(user.groups);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  function toggleGroup(group: string) {
    setGroups((current) =>
      current.includes(group)
        ? current.filter((item) => item !== group)
        : [...current, group],
    );
  }

  async function save(event: React.FormEvent) {
    event.preventDefault();
    if (groups.length === 0) {
      setError("그룹을 하나 이상 선택하세요.");
      return;
    }
    setBusy(true);
    setError("");
    try {
      onSaved(await updateCognitoUser(user.sub, { email, name, team, groups }));
    } catch (caught) {
      setError(errorMessage(caught, "사용자 정보 저장에 실패했어요."));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={save}>
      <div className="mb-4">
        <div className="truncate font-semibold">{user.name || user.email}</div>
        <div className="mt-1 break-all text-xs text-muted-foreground">{user.sub}</div>
      </div>
      <label className="mb-3 block">
        <span className="mb-1 block text-xs font-medium">이메일</span>
        <Input
          required
          type="email"
          autoComplete="email"
          value={email}
          onChange={(event) => setEmail(event.target.value)}
        />
      </label>
      <label className="mb-3 block">
        <span className="mb-1 block text-xs font-medium">이름</span>
        <Input required value={name} onChange={(event) => setName(event.target.value)} />
      </label>
      <label className="mb-3 block">
        <span className="mb-1 block text-xs font-medium">부서</span>
        <Input value={team} onChange={(event) => setTeam(event.target.value)} />
      </label>
      <fieldset className="mb-4">
        <legend className="mb-2 text-xs font-medium">그룹</legend>
        <div className="flex gap-5">
          {["user", "admin"].map((group) => (
            <label key={group} className="flex items-center gap-2 text-sm">
              <input
                type="checkbox"
                checked={groups.includes(group)}
                onChange={() => toggleGroup(group)}
                className="size-4 accent-primary"
              />
              {group}
            </label>
          ))}
        </div>
      </fieldset>
      {error && <p className="mb-3 text-sm text-red-700">{error}</p>}
      <div className="flex flex-wrap gap-2">
        <Button type="submit" size="sm" disabled={busy || user.status === "DISABLED"}>
          {busy ? "저장 중..." : "저장"}
        </Button>
        <Button
          type="button"
          variant="outline"
          size="sm"
          className="text-red-700"
          disabled={user.status === "DISABLED"}
          onClick={onOffboard}
        >
          비활성화
        </Button>
      </div>
    </form>
  );
}

function Status({ value }: { value: string }) {
  const disabled = value === "DISABLED";
  return (
    <span
      className={`inline-flex rounded-md px-2 py-0.5 text-xs font-medium ${
        disabled
          ? "bg-red-50 text-red-700"
          : "bg-emerald-50 text-emerald-700"
      }`}
    >
      {value}
    </span>
  );
}

function LoadingRows() {
  return (
    <div className="space-y-2" aria-label="사용자 불러오는 중">
      {[0, 1, 2, 3].map((row) => (
        <div key={row} className="h-14 animate-pulse rounded-md bg-muted/60" />
      ))}
    </div>
  );
}

function Message({
  children,
  tone = "muted",
}: {
  children: React.ReactNode;
  tone?: "muted" | "error";
}) {
  return (
    <div
      className={`rounded-lg border border-dashed px-4 py-12 text-center text-sm ${
        tone === "error" ? "border-red-200 text-red-700" : "text-muted-foreground"
      }`}
    >
      {children}
    </div>
  );
}

function errorMessage(error: unknown, fallback: string): string {
  return error instanceof Error && error.message ? error.message : fallback;
}
