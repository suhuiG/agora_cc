// API의 동기 invoke 예산(현재 300초)을 덮고, 요청 전달 지연을 위한 30초를 더 확보해요.
// API 값이 바뀌면 session-refresh.test.ts의 교차 언어 드리프트 가드가 이 값을 막아요.
export const TOKEN_REFRESH_MARGIN_SECONDS = 30;
export const REFRESH_WINDOW_SECONDS = 330;

export function shouldRefreshAccessToken(
  accessTokenExpiresAt: number,
  now: number,
): boolean {
  return accessTokenExpiresAt <= now + REFRESH_WINDOW_SECONDS;
}
