export type CognitoUserProfile = {
  email?: unknown;
  name?: unknown;
  preferred_username?: unknown;
  sub?: unknown;
};

function trimmedString(value: unknown): string | undefined {
  if (typeof value !== "string") {
    return undefined;
  }
  const normalized = value.trim();
  return normalized || undefined;
}

export function normalizeDisplayName(value: unknown): string | undefined {
  return trimmedString(value);
}

export function displayNameFromProfile(
  profile: CognitoUserProfile,
  expectedSubject: string,
): string | undefined {
  if (trimmedString(profile.sub) !== expectedSubject) {
    return undefined;
  }
  return (
    trimmedString(profile.email) ??
    trimmedString(profile.preferred_username) ??
    trimmedString(profile.name)
  );
}

// SRP 로그인의 access token엔 oauth2/userInfo용 scope가 없어 표시명 조회가 실패해요.
// id_token은 scope와 무관하게 사용자 속성(email/name)을 담으므로 여기서 표시명을 뽑아요.
export function displayNameFromIdToken(
  idToken: string | undefined,
): string | undefined {
  const token = trimmedString(idToken);
  if (!token) {
    return undefined;
  }
  const segments = token.split(".");
  if (segments.length < 2) {
    return undefined;
  }
  try {
    const claims = JSON.parse(
      Buffer.from(segments[1], "base64url").toString("utf8"),
    ) as CognitoUserProfile & { "cognito:username"?: unknown };
    return (
      trimmedString(claims.name) ??
      trimmedString(claims.preferred_username) ??
      trimmedString(claims["cognito:username"]) ??
      trimmedString(claims.email)
    );
  } catch {
    return undefined;
  }
}
