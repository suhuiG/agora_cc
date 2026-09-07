import { ComingSoon } from "@/components/ComingSoon";

export default function RuntimePage() {
  return (
    <ComingSoon
      domain="런타임"
      breadcrumb="런타임"
      summary="MCP·agent를 구동해 공용 endpoint를 제공해요. 로컬에서 만든 agent를 배포하면 AgentCore Runtime에서 실행되고, 단일 게이트웨이를 통해 복사 가능한 endpoint가 나와요."
      items={[
        "배포 — agent/MCP 컨테이너 구동 (ARM64, AgentCore Runtime)",
        "게이트웨이 — 단일 endpoint + tool 이름으로 라우팅, 인증·보안 일괄 적용",
        "endpoint — 복사 가능한 공용 주소 제공 (다른 사람·시스템이 공통 사용)",
        "agent형/app형 자동 분기 — 일반 앱은 별도 호스팅으로 라우팅",
      ]}
      source="Runtime + Gateway (단일 게이트웨이 target 집계)"
    />
  );
}
