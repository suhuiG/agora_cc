import { ComingSoon } from "@/components/ComingSoon";

export default function EvaluationPage() {
  return (
    <ComingSoon
      domain="평가"
      breadcrumb="평가"
      summary="등록 자산의 품질을 점수화하고 실행을 관찰해요. AgentCore Evaluations의 평가 파이프라인과 옵저버빌리티(트레이스·메트릭·비용)를 함께 담당해요."
      items={[
        "품질 — 온라인 샘플링·배치 평가, Builtin/Custom evaluator (Helpfulness·Correctness·GoalSuccessRate)",
        "트레이스 — OTel gen_ai.* span 트리, 토큰·지연 (옵저버빌리티 흡수)",
        "비용 — 세션별·전역 토큰/USD 누적, 이상 탐지",
      ]}
      source="Evaluation + Observability (CloudWatch GenAI · Langfuse)"
    />
  );
}
