import { Card } from "@/components/ui/card";

// "예정" 도메인의 placeholder 페이지예요.
// AgentOps Kit 흡수 항목을 어떤 순서로 붙일지 로드맵을 함께 보여줘요.
interface ComingSoonProps {
  domain: string;
  breadcrumb: string;
  summary: string;
  items: string[];
  source: string; // AgentOps Kit 어느 컴포넌트에서 흡수하는지
}

export function ComingSoon({ domain, breadcrumb, summary, items, source }: ComingSoonProps) {
  return (
    <div>
      <div className="mb-6">
        <div className="mb-1 text-xs text-muted-foreground">{breadcrumb}</div>
        <div className="flex items-center gap-2.5">
          <h1 className="text-2xl font-bold tracking-tight">{domain}</h1>
          <span className="rounded-full bg-amber-100 px-2 py-0.5 text-[11px] font-semibold text-amber-700">
            확장 예정
          </span>
        </div>
        <p className="mt-1 max-w-2xl text-muted-foreground">{summary}</p>
      </div>

      <Card className="max-w-2xl p-6">
        <div className="mb-3 text-sm font-semibold">예정 기능</div>
        <ul className="space-y-2">
          {items.map((it) => (
            <li key={it} className="flex items-start gap-2 text-sm text-muted-foreground">
              <span className="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full bg-slate-300" />
              {it}
            </li>
          ))}
        </ul>
        <div className="mt-5 border-t border-border pt-4 text-xs text-muted-foreground">
          레퍼런스: AgentOps Kit · <span className="font-medium text-foreground">{source}</span>
        </div>
      </Card>
    </div>
  );
}
