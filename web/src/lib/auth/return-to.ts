// returnTo 파라미터의 open-redirect 방어를 클라이언트·서버가 공유하는 순수 모듈이에요.
// 브라우저 API에 의존하지 않아 서버 route 에서도 그대로 import 할 수 있어요.

export const DEFAULT_RETURN_TO = "/catalog/browse";

function safeDecode(value: string): string {
  try {
    return decodeURIComponent(value);
  } catch {
    // 잘못된 %-인코딩은 신뢰할 수 없으니 원본을 그대로 검사해요.
    return value;
  }
}

/**
 * 앱 내부 경로만 허용해 cross-origin redirect 를 차단해요. 막는 케이스:
 * - "//host"       : protocol-relative URL
 * - "/\\host"      : 브라우저·new URL() 이 백슬래시를 "/" 처럼 처리해 cross-origin 이 됨
 * - "%2F%2F"·"%5C" : 인코딩된 슬래시/백슬래시로 위 검사를 우회 (decode 후 재검사)
 * - 제어문자        : 헤더/URL 파싱 혼란 유발
 * - "/login"        : 로그인 화면으로의 되돌이 방지
 */
export function safeReturnTo(value: string | null | undefined): string {
  if (!value) {
    return DEFAULT_RETURN_TO;
  }
  const decoded = safeDecode(value);
  const hasControlChar = Array.from(decoded).some(
    (ch) => ch.charCodeAt(0) < 0x20 || ch.charCodeAt(0) === 0x7f,
  );
  if (
    !decoded.startsWith("/") ||
    decoded.startsWith("//") ||
    decoded.startsWith("/\\") ||
    decoded.startsWith("/login") ||
    decoded.includes("\\") ||
    hasControlChar
  ) {
    return DEFAULT_RETURN_TO;
  }
  return value;
}
