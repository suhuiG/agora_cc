"""카탈로그 READ 도구에 회원 기본 권한을 부여해요 (ADR-0094).

⚠️ **ADR-0099 결정 6 이 이 모듈 전체를 폐기해요** — 자동 부여를 없애고 등록자가 도구별로
«호출 그룹» 을 고르게 바꿔요(IH-144). 그때까지 이 모듈이 시연·개발의 grant 공급원이에요.

## 무엇을 채우나 (ADR-0099 이후)

Gateway REQUEST interceptor 가 보는 사람 축은 **⑦ 한 층**이에요.

    ⑦ AccessGrant (사람 또는 그룹) — 키가 `(subject, asset_id, operation_id)`

이 모듈은 그 **도구별 행**을 채워요(`ensure_member_tool_grants`). 옛 모델의 ①~④
(`AssetCapability` · `Connection` · `ConnectionCapability` · ceiling)는 인가 경로에서
빠졌어요 — 라벨을 거쳐 자산으로 환전하던 그 층들이 IH-127·IH-130 의 원인이었어요.

`AssetCapability` 는 아직 **함께 써요.** 인가 판정에는 쓰이지 않고, 롤백 시 옛 층을 되살릴
역함수 조인표로 남겨 둔 것이에요(ADR-0099 §6.1 미루는 것). 지우면 인접 테스트가 함께
무너져서 이번 범위에서 빼 뒀어요.

## 경계 — READ 만이에요

`sensitivity == "READ"` 인 operation 만 자동 부여해요. CREATE·UPDATE·DELETE 는 손대지 않아요.
그게 이 자동화의 유일한 안전 경계예요 — 넓히면 "등록하면 누구나 쓸 수 있다" 가 돼요.

민감도는 Registry 의 태그에서 와요. **ADR-0094 결정 3 은 `heuristic_sensitivity` 의 추론값을
인가 근거로 쓰지 말라고 정해요** — 이름이 애매한 도구가 잘못 분류되면 조용히 열리니까요.

⚠️ **그런데 배포형 경로는 지금 그 결정을 지키지 않아요 (2026-09-05 실측).** 배포형 자산의
Registry 태그를 만든 것이 `runtime/deploy/tool_extract.py` → `heuristic_sensitivity()`,
즉 **바로 그 이름 추론**이에요. Registry·Target 원장을 거치는 건 저장 경로일 뿐이라
「명시 태그」라는 말이 근거의 출처를 바꾸지 않아요.

**규칙을 문장으로 완화하지 마세요.** ADR 이 금지한 것을 코드가 하고 있는 상태이고, 그건
기록해야 할 위반이지 재정의할 대상이 아니에요(`docs/06-risks.md` 09-05 IH-166 「기준 공백」).

모든 태그가 추론값인 건 **아니에요.** 출처가 실제로 갈려요 — 연결형은 MCP 자기 선언
(`descriptor`)을 보존할 수 있고, 남은 것만 이름 동사·Bedrock 분류·보수적 기본으로 채우고,
관리자가 `drift_router` 로 `admin` 출처로 확정할 수도 있어요. 행별 출처는 drift 원장의
`ToolLedgerEntry.sensitivity_source` 에 있고 어드민 승인 화면이 그걸 공시해요(IH-166).
그러니 **「전부 짐작이다」도 「명시 태그니 괜찮다」도 둘 다 틀려요** — 출처를 봐야 해요.

## 관리자의 거부를 덮지 않아요

이미 `REJECTED` 인 `AssetCapability` 는 그대로 둬요. 관리자가 일부러 막은 것이라, 재배포가
그걸 되살리면 승인 결정이 조용히 뒤집혀요. `PENDING` 도 덮지 않아요 — 심사 중이라는 뜻이에요.

## 재등록

새 버전에는 다시 부여해요. ADR-0090 은 "재등록이 이전 승인을 물려받지 않는다" 인데, 이건
물려받는 게 아니라 **새 버전의 태그를 다시 읽어 파생**하는 거예요. 이전 버전 행은 남겨두지
않고 현재 버전으로 갱신해요 — 버전이 어긋나면 interceptor 가 ①에서 거부해요.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from .models import (
    AccessGrant,
    AssetCapability,
    AssetCapabilityStatus,
    GrantStatus,
)
from .store import IdentityRecordNotFound, IdentityStore

_log = logging.getLogger(__name__)

#: `catalog_seed.CATALOG_PRESETS` 의 조회 전용 그룹이에요. 여기 리터럴을 두지 않고 그쪽에서
#: 읽으면 좋겠지만, import 하면 seed 모듈이 이 모듈에 의존하는 것처럼 보여요. 값이 갈라지지
#: 않도록 테스트가 두 곳을 대조해요.
READ_CONNECTION_ID = "preset-viewer"
READ_CAPABILITY = "data.read"

#: Agora 회원 전체를 뜻하는 Cognito 그룹. 어드민 콘솔의 사용자 관리 화면이 이 그룹을 다뤄요.
#:
#: **`admin` 도 포함해요.** 두 그룹은 상하 관계가 아니라 독립 플래그라서(`PLATFORM_ROLES`,
#: 사용자 관리 화면이 각각 토글해요) `admin` 만 가진 계정이 있을 수 있어요. 관리자도 Agora
#: 회원이니 회원 기본 권한을 못 받으면 안 돼요 — 2026-08-29 e2e 에서 `admin@agora.lab` 이
#: `admin` 만 갖고 있어 도구가 열리지 않았어요.
MEMBER_GROUPS = ("user", "admin")

#: 하위 호환 — 옛 이름을 참조하는 코드가 남아 있을 수 있어요.
MEMBER_GROUP = MEMBER_GROUPS[0]

#: 이 민감도만 자동 부여해요. 넓히지 마세요 — 넓히면 등록이 곧 전체 허용이 돼요.
#:
#: 이 상수는 **자동 부여**의 경계일 뿐이고, 관리자가 승인한 쓰기 capability 를 막지는 않아요.
#: 로컬 ZIP 으로 나가는 쪽의 천장은 별도예요 —
#: `dev_identity_service._DOWNLOAD_SENSITIVITY_CEILING` 이 승인 상태와 무관하게 잘라요.
AUTO_GRANT_SENSITIVITY = "READ"

def _member_grant_id(group: str, asset_id: str, operation_id: str) -> str:
    """행 하나에 하나 — 키가 (그룹, 자산, 도구) 라 id 도 그 셋에서 파생해요.

    id 는 이제 **정렬 키의 재료가 아니에요**(SK 는 `GRANT#<asset>#<op>`). 재실행이 같은 id 를
    다시 만들어야 감사 로그에서 같은 행으로 읽혀요.
    """
    return f"grant-member-{asset_id}-{operation_id}-{group}"


@dataclass
class ReadAccessReport:
    granted: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    #: 관리자가 이미 판단한 행(REJECTED·PENDING). 덮지 않았다는 걸 드러내요.
    preserved: list[str] = field(default_factory=list)
    #: READ 가 아니라 건너뛴 operation. 자동 부여의 경계를 화면이 보여줄 수 있게요.
    skipped_non_read: list[str] = field(default_factory=list)
    member_grant: str = ""
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def ensure_member_tool_grants(
    store: IdentityStore,
    *,
    asset_id: str,
    asset_version: str,
    operation_ids: tuple[str, ...],
    granted_by: str,
    now: str | None = None,
) -> str:
    """회원 그룹들에 **도구별** ⑦ grant 를 멱등하게 만들어요 (ADR-0099 결정 2).

    행 하나가 `(그룹, asset_id, operation_id)` 하나예요. 옛 모델은 그룹마다 `data.read` 라벨
    grant **한 행**이었고, 그 라벨을 요구하는 자산이 나중에 늘면 **부여하지 않은 자산까지 함께
    열렸어요**(IH-130). 이제 도구를 직접 가리켜서 그 환전이 없어요.

    반환값은 `group/operation=결과` 를 `,` 로 이은 요약이고, 결과는 `created` · `unchanged` ·
    `revoked_left_alone` 중 하나예요.

    **REVOKED 를 되살리지 않아요.** 관리자가 일부러 끊었을 수 있어요. 그룹·도구별로 독립
    판단이에요 — 한 도구를 끊었다고 다른 도구까지 막지 않아요.

    ## 판정은 소비자 경로로

    존재 판정은 `get_tool_grant()` — **interceptor 가 읽는 정확한 그 키**예요. `list_grants()`
    는 `principal_id=None` 일 때 전체 scan 이라 저장 위치를 무시해서, 2026-08-29 에 잘못된
    파티션의 행을 「있다」로 읽고 `unchanged` 를 돌려주는데 interceptor 는 전부 거부했어요.
    ADR-0099 가 정렬 키에도 의미를 실으니 같은 사고가 SK 에서 재현될 수 있어요.

    옛 SK 행(`GRANT#<connection>#<grant_id>`)은 이 조회에 **안 걸려요.** 그게 의도예요 —
    옛 행은 그대로 두고 새 경로에서 fail-closed 로 안 보이게 해요(ADR-0099 §6.1). 그래서
    파티션 잔재를 다시 쓰는 경로도 없앴어요: 옛 행에는 `asset_id`·`operation_id` 가 없어서
    옮겨 쓸 정보 자체가 없어요.
    """
    stamp = now or _now_iso()
    outcomes: list[str] = []
    for group in MEMBER_GROUPS:
        for operation_id in operation_ids:
            try:
                existing = store.get_tool_grant(
                    subject_group=group,
                    asset_id=asset_id,
                    operation_id=operation_id,
                )
            except IdentityRecordNotFound:
                existing = None
            if existing is not None:
                if existing.status is GrantStatus.ACTIVE:
                    outcomes.append(f"{group}/{operation_id}=unchanged")
                else:
                    _log.info(
                        "회원 grant 가 REVOKED 라 되살리지 않아요: group=%s asset=%s op=%s",
                        group, asset_id, operation_id,
                    )
                    outcomes.append(f"{group}/{operation_id}=revoked_left_alone")
                continue
            store.put_grant(AccessGrant(
                grant_id=_member_grant_id(group, asset_id, operation_id),
                principal_id="",
                status=GrantStatus.ACTIVE,
                version=1,
                granted_by=granted_by,
                created_at=stamp,
                updated_at=stamp,
                expires_at=None,
                subject_group=group,
                asset_id=asset_id,
                operation_id=operation_id,
                # 감사용이에요 — ④ 대조는 `ASSET#<id>`/`VERSION` 행이 담당해요(결정 13).
                asset_version=asset_version,
            ))
            _log.info(
                "회원 도구 grant 를 만들었어요: group=%s asset=%s op=%s by=%s",
                group, asset_id, operation_id, granted_by,
            )
            outcomes.append(f"{group}/{operation_id}=created")
    return ",".join(outcomes)


def provision_read_access(
    store: IdentityStore,
    *,
    asset_id: str,
    asset_version: str,
    operations: dict[str, str],
    granted_by: str,
    now: str | None = None,
) -> ReadAccessReport:
    """READ operation 에 `AssetCapability` 를 만들고 회원 grant 를 확인해요.

    `operations` 는 `{operation_id: sensitivity}` 예요 — Registry 의 **명시 태그**를 넘겨야
    해요. 이름 추론값을 넘기면 인가 근거가 추측이 돼요.
    """
    report = ReadAccessReport()
    stamp = now or _now_iso()

    for operation_id, sensitivity in sorted(operations.items()):
        if str(sensitivity or "").strip().upper() != AUTO_GRANT_SENSITIVITY:
            report.skipped_non_read.append(operation_id)
            continue
        try:
            current = store.get_asset_capability(asset_id, operation_id)
        except IdentityRecordNotFound:
            current = None
        if current is not None:
            if current.status is not AssetCapabilityStatus.APPROVED:
                # 관리자가 거부했거나 심사 중이에요. 덮지 않아요.
                report.preserved.append(f"{operation_id}:{current.status.value}")
                continue
            if (
                current.asset_version == asset_version
                and current.connection_id == READ_CONNECTION_ID
                and READ_CAPABILITY in current.required_capabilities
            ):
                report.unchanged.append(operation_id)
                continue
        store.put_asset_capability(AssetCapability(
            asset_id=asset_id,
            asset_version=asset_version,
            operation_id=operation_id,
            connection_id=READ_CONNECTION_ID,
            required_capabilities=(READ_CAPABILITY,),
            status=AssetCapabilityStatus.APPROVED,
            version=(current.version + 1) if current is not None else 1,
            updated_at=stamp,
            approved_by=granted_by,
        ))
        report.granted.append(operation_id)

    try:
        report.member_grant = ensure_member_tool_grants(
            store,
            asset_id=asset_id,
            asset_version=asset_version,
            # 이번 실행에서 실제로 열린 도구만이에요. `skipped_non_read` 와 `preserved` 는
            # 부여 대상이 아니라 그대로 둬요.
            operation_ids=tuple(report.granted + report.unchanged),
            granted_by=granted_by,
            now=stamp,
        )
    except Exception as exc:
        # grant 실패는 capability 부여를 되돌리지 않아요 — capability 만 있으면 아직
        # 아무도 못 불러요(⑤가 비어서 fail-closed). 다음 실행에서 다시 시도해요.
        report.member_grant = "failed"
        report.warnings.append(
            f"회원 그룹 grant 를 만들지 못했어요: {type(exc).__name__}: {exc}"
        )
    return report
