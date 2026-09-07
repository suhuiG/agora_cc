import type { ApprovalBlock } from "@/lib/api/types";
import {
  approvalBlockPresentation,
  TONE_CLASSES,
  type ApprovalBlockAudience,
} from "@/lib/trust";

type ApprovalBlockNoticeProps = {
  block?: ApprovalBlock | null;
  /** 등록자(나의 요청)와 관리자(승인 큐)는 할 수 있는 행동이 달라요. */
  audience?: ApprovalBlockAudience;
  /** 목록 셀처럼 좁은 자리에서는 제목 + 할 일 한 줄만 보여줘요. */
  compact?: boolean;
};

/**
 * 자동승인이 왜 진행되지 않았는지 + 무엇을 하면 풀리는지 (R3).
 *
 * 사유가 없으면 아무것도 그리지 않아요. **여기서 아무것도 안 보이는 것이 "승인됨"을
 * 뜻하지는 않아요** — 승인 여부는 옆의 심사/게시 상태가 보여줘요.
 */
export function ApprovalBlockNotice({
  block,
  audience = "owner",
  compact = false,
}: ApprovalBlockNoticeProps) {
  const view = approvalBlockPresentation(block, audience);
  if (!view) return null;

  if (compact) {
    return (
      <div
        role="note"
        aria-label={view.ariaLabel}
        className={`mt-1.5 rounded-md border px-2 py-1 text-[11px] leading-snug ${TONE_CLASSES[view.tone]}`}
      >
        <span className="font-semibold">{view.title}</span>
        {view.action && <span className="ml-1">{view.action}</span>}
      </div>
    );
  }

  return (
    <div
      role="note"
      aria-label={view.ariaLabel}
      className={`rounded-lg border px-3 py-2.5 text-[13px] leading-relaxed ${TONE_CLASSES[view.tone]}`}
    >
      <div className="font-semibold">자동 승인 보류 · {view.title}</div>
      {view.detail && <div className="mt-1">{view.detail}</div>}
      {view.action && <div className="mt-1 font-medium">{view.action}</div>}
      {block?.observed_at && (
        <div className="mt-1 text-[11px] opacity-70">관측 시각 {block.observed_at}</div>
      )}
    </div>
  );
}
