import type { ReactNode } from "react";
import { Card } from "@/components/ui/card";

// 거버넌스 Admin 각 화면의 "껍데기" placeholder.
// v0 단계: 레이아웃·진입만 구현하고, 상세 화면과 기능은 "개발 예정"으로 표기해요.
// planned = 이 화면에서 앞으로 붙일 기능 목록.
interface AdminPlaceholderProps {
  title: string;
  description: string;
  /** 앞으로 개발할 기능 목록. */
  planned: string[];
  /** 상단 우측 액션 영역 (버튼 등) — 지금은 비활성 표기용. */
  actions?: ReactNode;
}

export function AdminPlaceholder({ title, description, planned, actions }: AdminPlaceholderProps) {
  return (
    <div>
      <div className="mb-6 flex items-start justify-between gap-4">
        <div>
          <div className="flex items-center gap-2.5">
            <h1 className="text-2xl font-bold tracking-tight">{title}</h1>
            <span className="rounded-full bg-amber-100 px-2 py-0.5 text-[11px] font-semibold text-amber-700">
              개발 예정
            </span>
          </div>
          <p className="mt-1 max-w-2xl text-muted-foreground">{description}</p>
        </div>
        {actions ? <div className="flex shrink-0 items-center gap-2">{actions}</div> : null}
      </div>

      <Card className="max-w-2xl p-6">
        <div className="mb-3 text-sm font-semibold">이 화면에서 개발 예정인 기능</div>
        <ul className="space-y-2">
          {planned.map((it) => (
            <li key={it} className="flex items-start gap-2 text-sm text-muted-foreground">
              <span className="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full bg-slate-300" />
              {it}
            </li>
          ))}
        </ul>
        <div className="mt-5 border-t border-border pt-4 text-xs text-muted-foreground">
          화면 설계: 위키 <span className="font-medium text-foreground">Agora Governance Admin — UI &amp; 핵심 기능 설계 (v0)</span>
        </div>
      </Card>
    </div>
  );
}
