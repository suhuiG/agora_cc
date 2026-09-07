"use client";

// Agora 카탈로그에 등록된 MCP를 호출하는 Agent 소스의 형식 가이드(ADR-0025).
// 어느 파일에 어떤 MCP 정보가 있어야 Agora가 배포 시 MCP를 찾아 연결하는지 안내해요.
import { useState } from "react";

import { Modal } from "@/components/ui/modal";

const ROWS: Array<{ where: string; what: React.ReactNode }> = [
  {
    where: "agora-policy.json",
    what: (
      <>
        <span className="font-mono">version: 2</span>,{" "}
        <span className="font-mono">mcpAssets[].name</span> = 카탈로그 MCP
        이름(동일 필수), <span className="font-mono">operations[]</span> =
        사용할 operation ID, <span className="font-mono">endpoint</span> =
        Gateway 주소
      </>
    ),
  },
  {
    where: "agent-card.json",
    what: (
      <>
        <span className="font-mono">skills[]</span>에 MCP당 1개:{" "}
        <span className="font-mono">id</span>=<span className="font-mono">name</span>
        =MCP 이름, <span className="font-mono">tags:[&quot;mcp&quot;]</span>
      </>
    ),
  },
  {
    where: ".env  (.env.example 복사)",
    what: (
      <>
        <span className="font-mono">AGORA_MCP_ASSETS</span>(MCP endpoint 결속)·
        <span className="font-mono">COGNITO_*</span>(로컬 토큰). Agent가 시작 시
        로드, 배포는 Agora가 주입
      </>
    ),
  },
  {
    where: "agent/tools.py · agent/handler.py",
    what: <>위 env를 읽어 MCP 연결·검증 (코드 하드코딩 아님)</>,
  },
];

export function AgentFormatGuideButton() {
  const [open, setOpen] = useState(false);

  return (
    <>
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="inline-flex items-center gap-1.5 rounded-lg border border-border px-3 py-1.5 text-sm text-muted-foreground hover:bg-accent hover:text-foreground"
      >
        <span aria-hidden>ⓘ</span> Agora MCP 연동 Agent 형식 가이드
      </button>

      <Modal
        open={open}
        onClose={() => setOpen(false)}
        title="Agora MCP 연동 Agent 형식 가이드"
        description="카탈로그에 등록된 MCP를 Agent가 호출하려면 소스가 아래 형식이어야 해요."
        footer={
          <button
            type="button"
            onClick={() => setOpen(false)}
            className="rounded-lg border border-border px-3 py-1.5 text-sm hover:bg-accent"
          >
            닫기
          </button>
        }
      >
        <div className="space-y-3 text-sm text-card-foreground">
          <p>
            <span className="font-semibold">규칙 ①</span>{" "}
            <span className="font-mono">agora-policy.json</span>은{" "}
            <span className="font-mono">version: 2</span>이고, 각 MCP의{" "}
            <span className="font-mono">name</span>이 카탈로그 등록 MCP 이름과{" "}
            <span className="font-semibold">정확히 같아야</span> 해요. 배포 시
            Agora가 이 이름으로 승인된 최신 MCP를 찾아 Gateway endpoint를
            연결해요. 사용할 operation은{" "}
            <span className="font-mono">operations</span> 배열에 명시해요.
          </p>
          <p>
            <span className="font-semibold">규칙 ②</span> MCP endpoint·인증은
            코드에 하드코딩하지 말고 <span className="font-mono">.env</span>로
            주입해요. <span className="font-mono">.env.example</span>을{" "}
            <span className="font-mono">.env</span>로 복사해 값을 채우면 Agent가
            시작 시 로드하고(<span className="font-mono">agent/__init__.py</span>),
            배포 땐 Agora가 같은 값을 env로 자동 주입해요.
          </p>

          <div className="overflow-hidden rounded-lg border border-border">
            <table className="w-full text-left">
              <thead className="bg-muted/50 text-xs text-muted-foreground">
                <tr>
                  <th className="px-3 py-2 font-medium">위치</th>
                  <th className="px-3 py-2 font-medium">무엇을 담나</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {ROWS.map((row) => (
                  <tr key={row.where} className="align-top">
                    <td className="px-3 py-2 font-mono text-xs text-foreground whitespace-nowrap">
                      {row.where}
                    </td>
                    <td className="px-3 py-2 text-muted-foreground">
                      {row.what}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <p className="text-muted-foreground">
            이름이 policy·card·카탈로그에서 <span className="font-semibold">모두 같아야</span>{" "}
            배포·검증이 통과해요. 다르면 &quot;도구가 붙지 않았어요&quot;로
            실패해요.
          </p>
          <p className="text-xs text-muted-foreground">
            Initializr로 만든 Agent는 이 형식으로 자동 생성돼요.
          </p>
        </div>
      </Modal>
    </>
  );
}
