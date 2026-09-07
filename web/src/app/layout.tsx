import type { Metadata } from "next";
import { Geist } from "next/font/google";
import "./globals.css";
import { Providers } from "./providers";

// 루트 레이아웃은 html/body/Providers 골격만 담당해요.
// 화면 셸(상단바·사이드바)은 route group 별로 분리:
//  - (portal)  : 일반 포탈 — TopBar + Sidebar
//  - (console) : 거버넌스 Admin 콘솔 — 상단바 없이 AdminSidebar 만
const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: "Agora — Agent Marketplace",
  description: "사내 Agent/Skill/MCP 통합 포탈",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="ko" className={`${geistSans.variable} h-full antialiased`}>
      <body className="flex min-h-full flex-col bg-background text-foreground">
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
