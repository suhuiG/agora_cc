// MCP tool sensitivity 태그(READ/CREATE/UPDATE/DELETE) 배지.
// IA-22c(배포 등록 검토)와 IA-22f(Agent × Tool 매트릭스)가 공유해요 —
// 태그→색/라벨 매핑을 한 곳에만 두려고 뺐어요.
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/ui";

const SENSITIVITY_STYLES: Record<string, { label: string; className: string }> = {
  // READ = 안전(초록), CREATE = 파랑, UPDATE = 주황, DELETE = 빨강.
  READ: { label: "READ", className: "bg-emerald-100 text-emerald-700" },
  CREATE: { label: "CREATE", className: "bg-blue-100 text-blue-700" },
  UPDATE: { label: "UPDATE", className: "bg-amber-100 text-amber-800" },
  DELETE: { label: "DELETE", className: "bg-red-100 text-red-700" },
};

// 태그가 없거나("") 알 수 없는 값이면 중립 배지로 "미분류"를 보여줘요.
const UNTAGGED = { label: "미분류", className: "bg-slate-100 text-slate-600" };

export function SensitivityBadge({
  sensitivity,
  rationale,
  className,
}: {
  sensitivity?: string;
  rationale?: string;
  className?: string;
}) {
  const style = SENSITIVITY_STYLES[(sensitivity ?? "").toUpperCase()] ?? UNTAGGED;
  return (
    <Badge
      variant="type"
      className={cn("shrink-0", style.className, className)}
      title={rationale || undefined}
    >
      {style.label}
    </Badge>
  );
}
