import { JSON_HEADERS, request } from "./client";

export type CognitoUser = {
  sub: string;
  email: string;
  name: string;
  team: string;
  groups: string[];
  status: string;
  mfa_enabled: boolean;
};

export type CognitoUserPage = {
  items: CognitoUser[];
  next_page: string | null;
};

export type UserInviteInput = {
  email: string;
  name: string;
  team: string;
  groups: string[];
};

export type UserPatchInput = Partial<UserInviteInput>;

export type BulkUserRow = {
  line: number;
  email: string;
  action: string;
  errors: string[];
};

export type BulkUserResult = {
  total: number;
  passed: number;
  failed: number;
  rows: BulkUserRow[];
};

export function listCognitoUsers({
  query = "",
  group = "",
  page = "",
}: {
  query?: string;
  group?: string;
  page?: string;
}): Promise<CognitoUserPage> {
  const params = new URLSearchParams();
  if (query) params.set("q", query);
  if (group) params.set("group", group);
  if (page) params.set("page", page);
  const suffix = params.size ? `?${params}` : "";
  return request<CognitoUserPage>(`/api/admin/identity/users${suffix}`);
}

export function getCognitoUser(sub: string): Promise<CognitoUser> {
  return request<CognitoUser>(
    `/api/admin/identity/users/${encodeURIComponent(sub)}`,
  );
}

/**
 * `listAllCognitoUsers` 를 쓰는 화면들이 **공유하는** SWR 캐시 키예요 (IH-164).
 *
 * 이 walk 는 `next_page` 커서를 따라가는 순차 while 루프라 비용이 디렉터리 크기에
 * 비례해요. 화면마다 다른 키를 쓰면 `/admin/grants` → `/admin/agents` →
 * `/admin/audit-calls` 로 옮길 때마다 같은 walk 를 처음부터 다시 돌려요. 세 화면의
 * fetcher(`listAllCognitoUsers`) · SWR 옵션(전부 `providers.tsx` 의 전역 기본값) ·
 * 반환형(`CognitoUser[]`)이 같아서 한 키로 합쳐도 동작이 바뀌지 않아요.
 *
 * ⛔ **`ResponsibilityEditor` 의 `admin/identity/users/all-for-responsibility` 는
 * 이 키로 합치지 마세요.** 이유는 그 파일의 주석에 있어요 — 요약하면 fetcher 가
 * `listResponsibilityMembers`(다른 함수 · 다른 반환형 `DirectoryPage`)이고, 합치면
 * 미관측 공시 `truncated` 가 사라지고 DISABLED 회원이 후보가 되고 지연 walk 가
 * 진입 즉시 walk 로 바뀌어요.
 */
export const ALL_USERS_SWR_KEY = "admin/identity/users/all";

export async function listAllCognitoUsers(): Promise<CognitoUser[]> {
  const users: CognitoUser[] = [];
  const seenPages = new Set<string>();
  let page = "";

  while (!seenPages.has(page)) {
    seenPages.add(page);
    const result = await listCognitoUsers({ page });
    users.push(...result.items);
    if (!result.next_page) break;
    page = result.next_page;
  }

  return users;
}

export function inviteCognitoUser(
  body: UserInviteInput,
): Promise<CognitoUser> {
  return request<CognitoUser>("/api/admin/identity/users", {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify(body),
  });
}

export function updateCognitoUser(
  sub: string,
  body: UserPatchInput,
): Promise<CognitoUser> {
  return request<CognitoUser>(
    `/api/admin/identity/users/${encodeURIComponent(sub)}`,
    {
      method: "PATCH",
      headers: JSON_HEADERS,
      body: JSON.stringify(body),
    },
  );
}

export function disableCognitoUser(
  sub: string,
): Promise<{ sub: string; disabled: boolean; revoked_grants: number }> {
  return request(
    `/api/admin/identity/users/${encodeURIComponent(sub)}/disable`,
    { method: "POST" },
  );
}

export function validateBulkCognitoUsers(
  csv: string,
): Promise<BulkUserResult> {
  return request<BulkUserResult>(
    "/api/admin/identity/users/bulk:validate",
    {
      method: "POST",
      headers: JSON_HEADERS,
      body: JSON.stringify({ csv }),
    },
  );
}

export function commitBulkCognitoUsers(
  csv: string,
): Promise<BulkUserResult> {
  return request<BulkUserResult>("/api/admin/identity/users/bulk", {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify({ csv }),
  });
}
