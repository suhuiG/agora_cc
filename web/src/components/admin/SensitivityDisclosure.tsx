import { SensitivityBadge } from "@/components/SensitivityBadge";
import { Badge } from "@/components/ui/badge";
import {
  sensitivitySourceMessage,
  type SensitivityObservationStatus,
  type SensitivitySource,
} from "@/lib/sensitivitySource";
import { cn } from "@/lib/ui";

export function SensitivityDisclosure({
  sensitivity,
  source,
  status,
  className,
}: {
  sensitivity?: string;
  source?: SensitivitySource;
  status?: SensitivityObservationStatus;
  className?: string;
}) {
  const message = sensitivitySourceMessage(sensitivity, source, status);

  return (
    <div className={cn("flex min-w-0 flex-wrap items-center gap-1.5", className)}>
      {status === "unknown" ? (
        <Badge
          variant="type"
          className="shrink-0 bg-slate-200 text-slate-700"
        >
          등급 확인 못함
        </Badge>
      ) : (
        <SensitivityBadge sensitivity={sensitivity} />
      )}
      {message && (
        <p className="min-w-0 break-words text-[11px] text-muted-foreground">
          <span className="font-medium text-foreground">출처</span> {message}
        </p>
      )}
    </div>
  );
}
