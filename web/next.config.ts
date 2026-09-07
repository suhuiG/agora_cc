import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // App Runner 컨테이너 배포용 self-contained 산출물(.next/standalone/server.js).
  // 로컬 dev(`next dev`)에는 영향 없어요(빌드 산출 형태만 바꿔요).
  output: "standalone",
  // 동일 워크트리에서 독립 E2E 서버를 띄울 때 기존 dev 서버의 .next lock과 분리해요.
  distDir: process.env.NEXT_DIST_DIR || ".next",
  // 로컬 개발 시 127.0.0.1·localhost 양쪽에서 dev 리소스(HMR WebSocket 등) 접근을 허용해요.
  // 이게 없으면 127.0.0.1 로 접속했을 때 Next 16이 cross-origin dev 요청을 차단해
  // HMR WebSocket이 끊기고, 클라이언트 JS(onClick 등)가 제대로 동작하지 않아요.
  // Orca 워크스페이스는 dev 서버를 agora-N.orca.localhost 로 프록시하므로 그 서브도메인도
  // 허용해요(N은 워크스페이스마다 달라 와일드카드로 커버 — 공식 문서 지원 패턴).
  allowedDevOrigins: ["127.0.0.1", "localhost", "*.orca.localhost"],
  // 좌측 하단 dev 라우트 indicator를 숨겨요. 컴파일·런타임 에러는 계속 표시돼요(Next 16).
  devIndicators: false,
  async redirects() {
    return [
      {
        // D3(ADR-0111): 정책 인벤토리는 관리자 콘솔 셸 안으로 옮겨요.
        // `permanent: false`는 Next.js 16에서 메서드를 보존하는 307 응답이에요.
        source: "/governance/policies",
        destination: "/admin/policies",
        permanent: false,
      },
    ];
  },
};

export default nextConfig;
