import { JSON_HEADERS, request } from "./client";
import type { DirectoryMember, DirectoryPage } from "./directory";
import type {
  ResponsibilityHistory,
  ResponsibilityStatus,
  ResponsibilityUpdateInput,
} from "./types";
import { listCognitoUsers } from "./users";

const responsibilityPath = (recordId: string) =>
  `/api/assets/${encodeURIComponent(recordId)}/responsibility`;

export function getAssetResponsibility(
  recordId: string,
): Promise<ResponsibilityStatus> {
  return request<ResponsibilityStatus>(responsibilityPath(recordId));
}

export function updateAssetResponsibility(
  recordId: string,
  input: ResponsibilityUpdateInput,
): Promise<ResponsibilityStatus> {
  return request<ResponsibilityStatus>(responsibilityPath(recordId), {
    method: "PUT",
    headers: JSON_HEADERS,
    body: JSON.stringify(input),
  });
}

export function listAssetResponsibilityHistory(
  recordId: string,
): Promise<ResponsibilityHistory> {
  return request<ResponsibilityHistory>(
    `${responsibilityPath(recordId)}/history`,
  );
}

/**
 * 관리자 보정 화면의 회원 후보를 읽어요.
 *
 * ADR-0071의 `/api/directory/members`는 등록자 본인을 의도적으로 제외하지만, 이 화면은
 * 호출자인 admin 자신도 연락 담당자로 지정할 수 있어야 해요. 그래서 admin 전용 API를
 * 사용하되, 전체 projection 중 담당자 선택에 필요한 email·name만 즉시 남겨요.
 *
 * 모든 page를 끝까지 읽고, cursor가 반복돼 완결성을 증명할 수 없을 때만 truncated=true예요.
 * 중간 요청 실패는 부분 목록을 반환하지 않고 예외로 처리해 빈 검색 결과와 구분해요.
 */
export async function listResponsibilityMembers(): Promise<DirectoryPage> {
  const items: DirectoryMember[] = [];
  const seenEmails = new Set<string>();
  const seenPages = new Set<string>();
  let page = "";

  while (!seenPages.has(page)) {
    seenPages.add(page);
    const result = await listCognitoUsers({ page });
    for (const user of result.items) {
      const email = user.email.trim();
      const normalized = email.toLowerCase();
      if (
        user.status === "DISABLED" ||
        !email ||
        seenEmails.has(normalized)
      ) {
        continue;
      }
      seenEmails.add(normalized);
      items.push({ email, name: user.name });
    }
    if (!result.next_page) return { items, truncated: false };
    page = result.next_page;
  }

  return { items, truncated: true };
}
