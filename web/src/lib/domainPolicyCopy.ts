// 도메인 정책 화면(Cedar)의 문구·예시 — 값으로 단정할 수 있게 컴포넌트 밖에 둬요.
//
// `.tsx` 안에 리터럴로 두면 테스트가 못 읽어요 (web/ 에는 렌더 테스트 러너가 없어요).
// 그래서 「사실이 사라지지 않았다」를 지켜야 하는 문구는 전부 여기 상수로 있고,
// `domainPolicies.test.ts` 가 값으로 단정해요. 컴포넌트가 그 상수를 실제로 그리는지는
// 같은 테스트가 소스 텍스트로 확인해요.
//
// ⚠️ **UI 문구에는 근거 식별자(ADR 번호·티켓 번호·`file:line`)를 넣지 않아요.** 근거는 주석에
// 둬요 — 관리자 화면에 저장소 내부 좌표가 새면 읽는 사람이 확인할 수 없는 주장이 돼요.
// 이 파일 문구의 근거:
//   · `forbid` 가 인자 제거로 무력화되는 것과 `context.input` 이 열린 레코드인 것 —
//     ADR-0076, 실측 `docs/research/2026-08-26-ia45b-context-input-multi-tool-probe.md`
//   · 도구 인가는 Cedar 가 아니라 REQUEST interceptor 가 판정하는 것 — ADR-0075, ADR-0099
//   · `permit` 이 합집합이라 좁은 정책을 더해도 권한이 안 좁아지는 것 — ADR-0073, IH-154
//   · `has` 가드가 먼저 와서 인자가 없으면 default-deny 로 떨어지는 것 —
//     `api/src/agora/domains/identity/domain_policy.py` `build_when_clause`

// ── ⓐ 대상 Gateway ─────────────────────────────────────────────────────────
//
// 「자유 선택」을 만들지 않아요. 서버가 좌표를 **설정에서 도출**하고 (`shared/deps.py`
// `get_domain_policy_service`), 요청 본문에 gateway 필드가 아예 없어요. 화면이 목록을
// 하드코딩하면 관측하지 않은 것을 주장하는 셈이라, 선택기에는 **서버가 준 그 하나** 만 넣어요.

export const GATEWAY_FIELD_LABEL = "대상 Gateway";
export const GATEWAY_SINGLE_CHOICE_HINT =
  "대상 Gateway 는 하나예요 — 서버 설정에서 도출해요.";
export const GATEWAY_HELP_LABEL = "왜 Gateway 를 하나만 고를 수 있나요";
export const GATEWAY_HELP: readonly string[] = [
  "대상 Gateway 는 서버가 설정에서 도출해요. 화면은 좌표를 보내지 않아요 — 클라이언트가 Gateway 를 고를 수 있으면, 인가가 걸릴 자원을 클라이언트가 정하는 셈이거든요.",
  "policy engine 이 없는 Gateway 에는 Cedar 정책을 아예 만들 수 없어요. 그리고 REQUEST interceptor 가 붙지 않은 Gateway 는 도구 인가가 집행되지 않아서, 거기에 도메인 규칙을 만들면 Cedar 만 도는 반쪽이 돼요. 정책을 만들 수 있는 곳은 둘을 모두 갖춘 하나예요.",
  "이 목록은 서버가 알려준 좌표 그대로예요. 화면이 보지 못한 Gateway 를 목록에 적지 않아요.",
];
/** 서버가 좌표를 아직 안 줬을 때. 「없음」이 아니라 「미관측」이에요. */
export const GATEWAY_UNRESOLVED_LABEL = "확인 중";

/**
 * ARN 에서 사람이 읽는 Gateway 이름(= 자원 이름)을 뽑아요.
 *
 * 마지막 `/` 뒤를 그대로 써요. 뒤에 붙은 AgentCore 생성 id 를 잘라내지 «않아요» — 그 길이는
 * 규약이 아니라 관측이고, 추측해서 자르면 화면 이름이 실제 자원 이름과 달라져요.
 */
export function gatewayDisplayName(arn: string): string {
  const trimmed = arn.trim();
  if (!trimmed) return "";
  const slash = trimmed.lastIndexOf("/");
  return slash >= 0 ? trimmed.slice(slash + 1) : trimmed;
}

// ── ⓑ `?` 팝오버로 옮긴 설명 두 덩어리 ──────────────────────────────────────
//
// 화면에서 파란 박스를 걷어냈지만 **사실은 하나도 안 지웠어요.** 아래 세 사실이 반드시 남아요.
//   ⑴ `forbid … when { amount > 100000 }` 는 인자를 빼거나 이름을 바꾸면 평가 오류가 나서
//      그 금지가 적용되지 않고 통과해요.
//   ⑵ `context.input` 은 열린 레코드라 스키마에 없는 인자도 그대로 넘어가요(실측).
//   ⑶ 그래서 `permit` + `has` 가드만 만들고, 인자가 없으면 맞는 permit 이 없어 기본 거부예요.

/** PageHeading 설명 끝의 `?` — 「항상 permit + 가드 형태로만 만들어요」 */
export const PERMIT_SHAPE_HELP_LABEL = "permit + 가드 형태로만 만드는 이유";
export const PERMIT_SHAPE_HELP: readonly string[] = [
  "이 화면은 Cedar 원문을 받지 않아요. 자산·도구·인자·연산자·임계값만 고르면 서버가 permit 한 장으로 조립해요. 저장 전에 그 문장을 미리보기로 그대로 확인할 수 있어요.",
  "조립된 문장은 언제나 같은 모양이에요 — 조건절 맨 앞에 context.input has <인자> 가드가 오고, 그 뒤에 값 비교가 붙어요. 인자가 없으면 이 permit 이 안 맞고, 맞는 permit 이 없으면 Cedar 는 기본 거부예요.",
  "금지(forbid)는 만들지 않아요. 이유는 옆의 「왜 «금지» 가 아니라 «허용» 으로 쓰나요」 에 있어요.",
];

/** 옛 파란 박스의 제목 — 이제 이 문장 끝에 `?` 가 붙어요. */
export const WHY_PERMIT_QUESTION = "왜 «금지» 가 아니라 «허용» 으로 쓰나요";
export const WHY_PERMIT_HELP_LABEL = WHY_PERMIT_QUESTION;
/** 옛 파란 박스의 본문 — 한 글자도 잃지 않고 여기로 옮겼어요. */
export const WHY_PERMIT_HELP: readonly string[] = [
  "forbid … when { amount > 100000 } 로 쓰면, 봇이 amount 를 빼거나 이름을 바꾸는 것만으로 평가 오류가 나고 그 금지가 적용되지 않아 요청이 통과해요.",
  "context.input 이 열린 레코드라서 스키마에 없는 인자도 그대로 넘어가는 게 실측돼 있어요.",
  "그래서 이 화면은 permit + has 가드만 만들어요 — 인자가 없으면 맞는 permit 이 없어 기본 거부로 떨어져요.",
];

// ── ⓒ 「Cedar 적용 예시 보기」 — 케이스 3가지 ───────────────────────────────
//
// 세 케이스 모두 **이 화면의 폼이 실제로 만들 수 있는 것** 이에요. 근거:
//   · 인자는 그 도구가 실제로 선언한 것이에요 (`sample/order-table-mcp/.../server.py`).
//   · 연산자·값 종류는 서버 allowlist 안이에요 (`domain_policy.py` `NUMERIC_OPERATORS`,
//     `STRING_OPERATORS`, `VALUE_NUMBER`/`VALUE_STRING`).
//   · Cedar 문장은 `compile_domain_rule` 이 만드는 모양 그대로예요.
// 테스트가 이 셋을 각각 **다른 소유자의 파일** 에서 읽어 대조해요.
//
// ⚠️ 「누가 무엇을 부를 수 있나」를 Cedar 로 푸는 예시는 넣지 않아요. 그건 도구 인가 승인과
// interceptor 소관이고, 이 화면의 존재 이유가 그 구분이에요 (ADR-0075·ADR-0099).

export const EXAMPLES_BUTTON_LABEL = "Cedar 적용 예시 보기";
export const EXAMPLES_MODAL_TITLE = "Cedar 적용 예시 — 케이스 3가지";
export const EXAMPLES_SCOPE_NOTE =
  "세 케이스 모두 이 화면의 폼으로 그대로 만들 수 있어요. 「누가 이 도구를 부를 수 있나」는 여기서 만들지 않아요 — 그건 도구 인가 승인과 Gateway interceptor 가 원장을 읽어 판정해요.";
export const EXAMPLES_ACTION_NOTE =
  "action 이름의 앞부분은 그 도구가 속한 Gateway Target 이에요. 자산과 도구를 고르면 서버가 채워요.";
export const EXAMPLES_MODE_NOTE =
  "만든 규칙은 LOG_ONLY 로 시작해요. 아래 문장이 실제로 강제되려면 목록에서 ACTIVE 로 승격해야 해요.";
/** 서버 좌표를 아직 못 읽었을 때 Cedar 예시의 `resource` 자리에 넣어요. */
export const EXAMPLE_GATEWAY_PLACEHOLDER = "<서버가 채우는 Gateway ARN>";

/** 폼에서 고르는 값 한 줄. `field` 는 이 화면 폼의 라벨과 같은 낱말이어야 해요. */
export interface ExampleFormEntry {
  field: string;
  value: string;
}

export interface DomainPolicyExample {
  id: string;
  /** ⒜ 상황 한 줄. */
  situation: string;
  /** ⒝ 이 화면에서 어떻게 만드는지 — 폼 필드 그대로예요. */
  form: readonly ExampleFormEntry[];
  /** 예시가 쓰는 자산·도구·인자 (테스트가 실제 MCP 선언과 대조해요). */
  asset: string;
  tool: string;
  argument: string;
  /** ⒞ 생성되는 Cedar 문장. */
  cedar: string;
  /** 그 문장이 실제로 무엇을 막고 무엇을 함께 막는지. */
  caution: string;
}

function cedarStatement(action: string, when: string, gatewayArn: string): string {
  const gateway = gatewayArn.trim() || EXAMPLE_GATEWAY_PLACEHOLDER;
  return [
    "permit(",
    "  principal is AgentCore::OAuthUser,",
    `  action == AgentCore::Action::"${action}",`,
    `  resource == AgentCore::Gateway::"${gateway}"`,
    ")",
    `when { ${when} };`,
  ].join("\n");
}

/**
 * 케이스 3가지. `gatewayArn` 은 **서버가 준 값** 이에요 — 화면이 좌표를 만들지 않아요.
 *
 * 셋을 이렇게 골랐어요.
 *   ① 숫자 상한 — 대표 케이스(금액 가드)예요.
 *   ② 문자열 동등 — 값 종류가 다르고, `permit` 이 합집합이라는 성질을 설명해야 하는 자리예요.
 *   ③ 다시 숫자 상한이지만 **`has` 가드의 대가** 를 보여줘요. 인자를 안 보내는 호출까지 막혀요.
 */
export function domainPolicyExamples(
  gatewayArn: string,
): readonly DomainPolicyExample[] {
  return [
    {
      id: "refund_amount_cap",
      situation:
        "반품 신청에 붙는 환불 금액이 100,000원을 넘지 않게 하고 싶어요.",
      form: [
        { field: "자산", value: "order-table-mcp" },
        { field: "도구", value: "request_return" },
        { field: "인자 이름", value: "refund_amount" },
        { field: "값 종류", value: "숫자 (정수)" },
        { field: "연산자", value: "<=" },
        { field: "임계값", value: "100000" },
      ],
      asset: "order-table-mcp",
      tool: "request_return",
      argument: "refund_amount",
      cedar: cedarStatement(
        "order-table-mcp-update___request_return",
        "context.input has refund_amount && context.input.refund_amount <= 100000",
        gatewayArn,
      ),
      caution:
        "100,000원을 넘는 호출은 맞는 permit 이 없어 거부돼요. 봇이 금액을 낮춰 적어 우회할 이유는 없어요 — 낮게 적으면 그만큼만 환불되니까요.",
    },
    {
      id: "status_pin",
      situation:
        "주문 수정 도구로는 환불 확정만 하게 하고, 다른 상태로 바꾸는 호출은 막고 싶어요.",
      form: [
        { field: "자산", value: "order-table-mcp" },
        { field: "도구", value: "update_order" },
        { field: "인자 이름", value: "status" },
        { field: "값 종류", value: "문자열" },
        { field: "연산자", value: "==" },
        { field: "임계값", value: "REFUNDED" },
      ],
      asset: "order-table-mcp",
      tool: "update_order",
      argument: "status",
      cedar: cedarStatement(
        "order-table-mcp-update___update_order",
        'context.input has status && context.input.status == "REFUNDED"',
        gatewayArn,
      ),
      caution:
        "문자열은 == 하나만 받아요. 허용할 값이 둘이면 값마다 규칙을 하나씩 만들어요 — permit 은 합집합이라 두 장이 함께 열어 줘요. 거꾸로, 이미 이 도구를 허용하는 넓은 permit 이 살아 있으면 이 규칙은 아무것도 좁히지 못해요.",
    },
    {
      id: "list_limit_cap",
      situation: "한 번에 읽어 가는 주문 건수를 50건으로 묶고 싶어요.",
      form: [
        { field: "자산", value: "order-table-mcp" },
        { field: "도구", value: "list_orders" },
        { field: "인자 이름", value: "limit" },
        { field: "값 종류", value: "숫자 (정수)" },
        { field: "연산자", value: "<=" },
        { field: "임계값", value: "50" },
      ],
      asset: "order-table-mcp",
      tool: "list_orders",
      argument: "limit",
      cedar: cedarStatement(
        "order-table-mcp-read___list_orders",
        "context.input has limit && context.input.limit <= 50",
        gatewayArn,
      ),
      caution:
        "has 가드가 먼저 오니, limit 을 아예 안 보내는 호출은 도구에 기본값이 있어도 이 permit 에 안 맞아 거부돼요. 상한 규칙은 봇이 그 인자를 항상 보내는 도구에 거는 편이 안전해요.",
    },
  ];
}
