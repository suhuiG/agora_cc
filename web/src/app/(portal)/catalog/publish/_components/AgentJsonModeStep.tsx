import type { ChangeEventHandler } from "react";

import { textareaClass } from "./styles";

type AgentJsonModeStepProps = {
  cardText: string;
  cardValid: boolean;
  onCardTextChange: (value: string) => void;
  onPickCard: ChangeEventHandler<HTMLInputElement>;
};

export function AgentJsonModeStep({
  cardText,
  cardValid,
  onCardTextChange,
  onPickCard,
}: AgentJsonModeStepProps) {
  return (
    <>
      <p className="text-slate-600 mb-2">
        A2A 에이전트의{" "}
        <span className="font-mono text-slate-700">agent-card.json</span>{" "}
        카드를 올려 주세요.
      </p>
      <p className="text-sm text-slate-500 mb-4">
        카드를 붙여넣거나 파일로 불러오면 이름·설명을 자동으로 채워요.
      </p>

      <div className="border border-slate-200 rounded-lg p-4">
        <div className="flex items-center justify-between mb-2">
          <span className="text-sm font-medium text-slate-700">
            agent-card.json
          </span>
          <label className="text-xs px-2 py-1 border border-slate-300 rounded-lg hover:bg-slate-50 cursor-pointer">
            파일 불러오기
            <input
              type="file"
              accept=".json,application/json"
              onChange={onPickCard}
              className="hidden"
            />
          </label>
        </div>
        <textarea
          value={cardText}
          onChange={(event) => onCardTextChange(event.target.value)}
          placeholder='agent-card.json 내용을 붙여넣거나 파일을 불러오세요. 예: {"name":"my-agent","protocolVersion":"0.3.0",...}'
          rows={8}
          className={textareaClass}
        />
        {cardText.trim() && !cardValid && (
          <p className="text-xs text-red-600 mt-1">
            유효한 JSON이 아니거나 name이 없어요.
          </p>
        )}
        {cardValid && (
          <p className="text-xs text-emerald-700 mt-1">
            ✅ 유효한 agent-card예요.
          </p>
        )}
      </div>
    </>
  );
}
