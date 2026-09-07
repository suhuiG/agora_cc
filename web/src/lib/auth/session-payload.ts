export type SessionPayload = {
  access_token: string;
  refresh_token: string;
  expires_in: number;
};

export function parseSessionPayload(raw: unknown): SessionPayload | null {
  if (!raw || typeof raw !== "object") {
    return null;
  }

  const value = raw as Record<string, unknown>;
  const accessToken = value.access_token;
  const refreshToken = value.refresh_token;
  const expiresIn = Number(value.expires_in);
  if (
    typeof accessToken !== "string" ||
    typeof refreshToken !== "string" ||
    !Number.isFinite(expiresIn) ||
    expiresIn <= 0
  ) {
    return null;
  }

  return {
    access_token: accessToken,
    refresh_token: refreshToken,
    expires_in: expiresIn,
  };
}
