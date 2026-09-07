import { inputClass, labelClass } from "./styles";

type McpConnectModeStepProps = {
  endpoint: string;
  connecting: boolean;
  connectVerified: boolean;
  tools: { name: string; description: string }[];
  onEndpointChange: (value: string) => void;
  onConnect: () => void;
};

export function McpConnectModeStep({
  endpoint,
  connecting,
  connectVerified,
  tools,
  onEndpointChange,
  onConnect,
}: McpConnectModeStepProps) {
  return (
    <>
      <p className="text-slate-600 mb-4">
        이미 호스팅 중인 MCP 서버의 endpoint 를 입력해 주세요. connect 테스트에
        통과하면 카탈로그에 등재돼요.
      </p>
      <div className="space-y-3">
        <label className={labelClass}>MCP endpoint URL</label>
        <div className="flex gap-2">
          <input
            value={endpoint}
            onChange={(event) => onEndpointChange(event.target.value)}
            placeholder="https://.../mcp"
            className={inputClass}
          />
          <button
            type="button"
            onClick={onConnect}
            disabled={connecting || !endpoint.trim()}
            className="px-4 py-2 bg-slate-900 text-white rounded-lg hover:bg-slate-700 disabled:opacity-40 whitespace-nowrap"
          >
            {connecting ? "연결 중..." : "connect"}
          </button>
        </div>
        {connectVerified && (
          <div className="text-sm text-emerald-700 bg-emerald-50 p-2 rounded">
            ✅ 연결 성공 — tool: {tools.length}개
          </div>
        )}
        {tools.length > 0 && (
          <div className="flex flex-wrap gap-1.5 mt-2">
            {tools.map((tool) => (
              <code
                key={tool.name}
                className="font-mono text-xs text-slate-700 bg-slate-100 px-2 py-1 rounded"
              >
                {tool.name}
              </code>
            ))}
          </div>
        )}
      </div>
    </>
  );
}
