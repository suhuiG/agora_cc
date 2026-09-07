import { JSON_HEADERS, request } from "./client";

/**
 * 담당자 지정용 회원 검색 (CA-29 · ADR-0071).
 *
 * 관리자 사용자 API(`/api/admin/identity/users`)를 쓰지 않아요 — 그건 admin 전용이고
 * `sub`·groups·status·MFA 까지 돌려줘요. 이 API 는 email·name 만 주는 최소 투영이고
 * 일반 등록자도 호출할 수 있어요.
 */
export type DirectoryMember = {
  email: string;
  name: string;
};

export type DirectoryPage = {
  items: DirectoryMember[];
  /** 상한(10건)에 걸려 잘렸는지. 잘린 목록을 전체처럼 보여주면 없는 사람이라고 오해해요. */
  truncated: boolean;
};

/** 이름·email 로 회원을 검색해요. 검색어는 2자 이상이어야 하고, 본인은 결과에서 빠져요. */
export async function searchDirectoryMembers(
  query: string,
): Promise<DirectoryPage> {
  return request<DirectoryPage>(
    `/api/directory/members?q=${encodeURIComponent(query)}`,
    { headers: JSON_HEADERS },
  );
}
