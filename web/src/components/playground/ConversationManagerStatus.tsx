import {
  conversationManagerLabel,
  type ConversationManagerObservation,
} from "@/lib/conversationManager";

export function ConversationManagerStatus({
  manager,
  compact = false,
}: {
  manager: ConversationManagerObservation | null;
  compact?: boolean;
}) {
  // 부재가 아니라 미관측이에요 — 「없음」으로 접지 않아요(ADR-0037 §4). 문구는
  // `lib/playground/runConfig.ts` 의 `UNOBSERVED_LABEL` 과 같은 말로 맞춰요.
  if (manager === null) {
    return (
      <span className="text-xs text-slate-400">
        실체 미관측
      </span>
    );
  }
  return (
    <span className={compact ? "text-xs text-slate-600" : "text-sm text-slate-700"}>
      {conversationManagerLabel(manager)}
    </span>
  );
}
