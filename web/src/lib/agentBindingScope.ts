/**
 * ④-only 화면의 경계 고지.
 *
 * 두 화면은 binding 승인만 쓰고 ⑦ grant 를 만들거나 정확-SK 로 확인하지 않아요. READ
 * 자동 부여도 배포 job 의 best-effort 단계라 이 문구에서 per-row 결과를 보증하지 않아요.
 */
export const AGENT_BINDING_SCOPE_NOTICE = {
  title: "이 화면은 ④ agent tool binding만 다뤄요.",
  body:
    "여기서 승인해도 사람이 실제로 호출할 수 있다는 뜻은 아니에요. " +
    "호출자의 사람·그룹 권한은 별도 ⑦ grant이고, 이 화면에서는 그 상태를 확인하지 않아요.",
  followUp:
    "⑦은 «도구 인가 승인»에서 같은 신청 행을 펼쳐 확인해 주세요.",
  href: "/admin/tool-authorization",
  linkLabel: "도구 인가 승인에서 ⑦ 확인",
} as const;
