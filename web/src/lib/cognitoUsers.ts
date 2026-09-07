import type { CognitoUser } from "./api/users";

export function cognitoUserLabel(user: CognitoUser): string {
  return user.email.trim() || user.name.trim() || "이름 없는 사용자";
}

export function filterCognitoUsers(
  users: CognitoUser[],
  query: string,
): CognitoUser[] {
  const normalized = query.trim().toLocaleLowerCase();
  if (!normalized) return users;

  return users.filter((user) =>
    [user.name, user.email].some((value) =>
      value.toLocaleLowerCase().includes(normalized),
    ),
  );
}
