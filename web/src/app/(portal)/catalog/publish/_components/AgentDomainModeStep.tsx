import type { AgentConnectTestResult } from "@/lib/api";

import { inputClass } from "./styles";

type AgentDomainModeStepProps = {
  domain: string;
  connecting: boolean;
  card: AgentConnectTestResult | null;
  error: string;
  onDomainChange: (value: string) => void;
  onConnect: () => void;
};

export function AgentDomainModeStep({
  domain,
  connecting,
  card,
  error,
  onDomainChange,
  onConnect,
}: AgentDomainModeStepProps) {
  return (
    <>
      <p className="text-slate-600 mb-2">
        이미 떠 있는 A2A 에이전트의 도메인을 연결해 주세요.
      </p>
      <p className="text-sm text-slate-500 mb-4">
        connect 테스트로 agent-card를 확인한 뒤 등재돼요.
      </p>

      <div className="border border-slate-200 rounded-lg p-4">
        <span className="text-sm font-medium text-slate-700 mb-2 block">
          도메인 연결
        </span>
        <div className="flex gap-2">
          <input
            value={domain}
            onChange={(event) => onDomainChange(event.target.value)}
            placeholder="https://agent.example.com (또는 agent-card.json URL)"
            className={inputClass}
          />
          <button
            type="button"
            onClick={onConnect}
            disabled={connecting || !domain.trim()}
            className="px-4 py-2 bg-slate-900 text-white rounded-lg hover:bg-slate-700 disabled:opacity-40 whitespace-nowrap"
          >
            {connecting ? "연결 중..." : "connect"}
          </button>
        </div>
        <p className="text-xs text-slate-400 mt-1">
          도메인만 주면{" "}
          <span className="font-mono">/.well-known/agent-card.json</span>을
          자동으로 찾아요.
        </p>
        {error && (
          <div className="text-sm text-red-600 bg-red-50 p-2 rounded mt-2">
            {error}
          </div>
        )}
        {card && (
          <div className="mt-3 space-y-2">
            <div className="text-sm text-emerald-700 bg-emerald-50 p-2 rounded">
              ✅ 연결 성공 — {card.name} · v{card.version} · A2A{" "}
              {card.protocol_version}
            </div>
            {card.skills.length > 0 && (
              <div>
                <div className="text-xs text-slate-500 mb-1">
                  skills {card.skills.length}개
                </div>
                <div className="flex flex-wrap gap-1.5">
                  {card.skills.map((skill) => (
                    <code
                      key={skill.id}
                      className="font-mono text-xs text-slate-700 bg-slate-100 px-2 py-1 rounded"
                      title={skill.description}
                    >
                      {skill.name}
                    </code>
                  ))}
                </div>
              </div>
            )}
            {card.security_schemes.length > 0 && (
              <div className="text-xs text-amber-700 bg-amber-50 p-2 rounded">
                🔒 이 에이전트는 인증을 요구해요:{" "}
                {card.security_schemes.join(", ")}
              </div>
            )}
          </div>
        )}
      </div>
    </>
  );
}
