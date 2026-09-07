from __future__ import annotations

import re
from dataclasses import dataclass, field

ALLOWED_LICENSES = ("MIT", "Apache-2.0", "LGPL-2.1", "LGPL-3.0", "BSD-3-Clause")


def slugify(name: str) -> str:
    s = name.strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


def is_license_allowed(lic: str) -> bool:
    return lic in ALLOWED_LICENSES


@dataclass
class GovTool:
    tool_id: str
    name: str
    area: str
    repo: str
    license: str
    exec_kind: str            # offline | cli | service
    invoke_cmd: str
    timeout: int
    finding_risk_map: dict
    target_asset_types: tuple
    status: str               # active | staged
    current_version: str
    # --- aws backend 실행 속성 (SP-2) ---
    compute: str = "lambda"           # lambda | fargate
    compute_rationale: str = ""       # LLM/규칙이 compute를 정한 근거
    image_uri: str = ""               # ECR repo:tag (SP-3에서 채움)
    # 소스 번들(업로드된 파일)이 있어야 의미가 있는 도구인지. gitleaks·semgrep·trivy는 True,
    # llm-judge는 False(자산 descriptor — MCP tool 설명·agent card만으로 판정 가능).
    # 소스 없는 자산(connect형 MCP·도메인 연결 agent)에서 needs_source 도구는 실행하지 않고
    # 게이트를 not_applicable로 표시해요 — 빈 입력 스캔을 "통과"로 오인하지 않도록.
    needs_source: bool = True


@dataclass
class TierCell:
    tier: str                 # minimal | standard | strong
    tool_id: str
    enforcement: str          # required | warn | off
    threshold: str = ""
    asset_type_scope: str = "*"


@dataclass
class ConsoleSettings:
    auto_scan: bool = False
    # 등록 직후 중복/유사 자산 후보를 자동 탐색할지. 스캔과 별개 토글이에요 — 중복검토는
    # Registry 기존 데이터만 읽는 저비용 분석이라 스캔(Fargate/StepFn 비용)과 수명주기가 달라요.
    #
    # **기본 ON**이에요(스캔은 기본 OFF). 비용 부담이 없고, 등록 시점에 "같은 일을 하는 자산이
    # 이미 있는지" 모르면 중복 자산이 쌓이는 걸 막을 수 없어요. 끄고 싶으면 콘솔 설정에서 꺼요.
    auto_overlap_review: bool = True
    auto_detect_rate: str = "P1W"
    updated_by: str = ""
    updated_at: str = ""
    # 에셋 타입(skill/mcp/agent) → 스캔 적용 등급. 게이트 SoT (SP-1).
    asset_tier_map: dict = field(default_factory=lambda: {
        "skill": "minimal", "mcp": "standard", "agent": "strong",
    })
    # 등급별 LLM-judge Bedrock 모델 별칭. admin이 콘솔에서 교체 가능.
    judge_model_map: dict = field(default_factory=lambda: {
        "minimal": "haiku-4-5", "standard": "sonnet-4-6", "strong": "sonnet-5",
    })


@dataclass
class ScanRecord:
    ts: str
    risk: str                 # none | low | medium | high
    findings: list = field(default_factory=list)
    trigger: str = "manual-queue"   # auto | manual-queue | manual-detail
    principal: str = ""
    status: str = "done"      # queued | running | done | failed
    # 이 스캔이 실제로 검사한 도구 area 목록(secret·sast 등). 게이트가 "finding 없음"을
    # 진짜 통과(검사함)와 미실행(검사 안 함)으로 구분하는 근거. 빈 리스트면 하위호환(기존 동작).
    scanned_areas: list = field(default_factory=list)
    scanner_kind: str = ""   # "" = 레거시(StaticScanner 하위호환). "stepfn"/"fargate" = area 기반 엄격 판정.
    # stepfn 실행 이름(=SF execution name). 재스캔마다 유니크해야 ExecutionAlreadyExists 없이
    # 새 실행이 돌아요. 폴링(sync_running_stepfn)이 이 값으로 describe_execution을 조회해요.
    # 빈 문자열이면 하위호환(record_id를 scan_id로 쓰던 단순 규약).
    scan_id: str = ""
    # 이 스캔이 검사한 자산 버전. 재배포(코드 교체) 후 "스캔 기록이 있다"는 이유로
    # 새 코드가 무검사 통과하는 걸 막는 근거예요(실측 2026-07-27). 빈 값이면 하위호환.
    version: str = ""
    # 부분 재스캔(단일 도구 재시도)일 때, 이번에 재실행하는 도구의 area. running placeholder에
    # 실어 merge 시점에 "이건 부분 재스캔이고 base가 있다"를 알려요(base findings/scanned_areas는
    # 같은 레코드의 findings/scanned_areas에 실어둠). 빈 값이면 전체 스캔(무회귀). done 레코드로
    # 굳힐 땐 ""로 리셋 — 완결된 전체 스냅샷이 되도록.
    rescan_area: str = ""


@dataclass(frozen=True)
class MonitoringScanProjection:
    """Body-free latest scan metadata for admin monitoring."""

    ts: str
    risk: str
    status: str
    version: str

    def to_dict(self) -> dict:
        return {
            "ts": self.ts,
            "risk": self.risk,
            "status": self.status,
            "version": self.version,
        }


@dataclass
class OverlapCandidate:
    """중복검토가 찾은 유사 자산 1건.

    `record_id`는 후보 자산의 Registry record_id예요. 후보가 나중에 삭제될 수 있으니
    `name`·`asset_type`을 스냅샷으로 함께 남겨요(조회 실패해도 무엇과 겹쳤는지 남아야 감사가 돼요).
    """

    record_id: str
    name: str
    asset_type: str = ""
    version: str = ""
    score: int = 0            # 0~100
    band: str = "low"         # high(>=70) | medium(40-69) | low(<40)
    # 점수에 기여한 근거. "무엇이 겹쳤는지"를 사람이 읽을 수 있어야 검토가 가능해요.
    reasons: list = field(default_factory=list)


@dataclass
class OverlapRecord:
    """중복검토 결과 1회분 (OVERLAP#{record_id} / TS#{ts}).

    ScanRecord와 같은 append 규약이에요. `status`가 `done`이어도 `candidates`가 비어 있을 수
    있어요 — 그건 "검토했고 중복 없음"이고, 레코드 자체가 없는 건 "아직 검토 안 함"이에요.
    이 둘을 구분해야 미검토를 "중복 없음"으로 오인하지 않아요(ScanRecord의 scan_state 4-enum과 같은 취지).
    """

    ts: str
    status: str = "done"      # running | done | failed
    trigger: str = "auto"     # auto | manual
    principal: str = ""
    candidates: list = field(default_factory=list)
    # 이 검토가 본 자산 버전. 재배포로 내용이 바뀌면 다시 검토해야 하는 근거예요
    # (ScanRecord.version과 같은 이유).
    version: str = ""
    # 비교 대상 모집단 크기. 0이면 "비교할 자산이 없었다"라서 후보 0건의 의미가 달라요.
    compared: int = 0
    error: str = ""

    @property
    def top_band(self) -> str:
        """가장 높은 후보의 band. 후보가 없으면 "none"."""
        for band in ("high", "medium", "low"):
            if any(c.get("band") == band if isinstance(c, dict) else c.band == band
                   for c in self.candidates):
                return band
        return "none"


@dataclass
class DecisionRecord:
    """승인/반려 판정 감사 기록 (§3-M2 F8, AUX#{id}/DECISION#{ts})."""
    ts: str
    principal: str
    decision: str             # APPROVE | REJECT
    reason: str = ""
    override: bool = False    # 게이트 미통과인데 승인(soft override)이면 True


@dataclass
class ApprovalBlockRecord:
    """자동승인이 진행되지 않은 사유 1건 (GOV#APPROVALBLOCK / REC#{id}).

    자산마다 **최신 1건만** 남겨요 — "지금 무엇 때문에 대기 중인가"를 답하는 값이라
    이력이 아니라 현재 상태예요(판정 이력은 DecisionRecord가 append-only로 담당).
    자동승인이 성공하면 지워요. 기록이 없다는 건 "막힌 적 없음"이고, 승인됐다는
    뜻이 아니에요 — 승인 여부는 Registry status가 정본이에요.

    `reason`은 `shared.trust.ApprovalBlockReason` 값 문자열이에요. 문자열로 저장하는
    이유는 원장이 enum 정의보다 오래 살아서, 나중에 코드에서 사유를 지워도 옛 기록을
    읽을 수 있어야 하기 때문이에요.
    """

    ts: str
    reason: str
    detail: str = ""
    remediation: str = ""
    # 비어 있는 담당자 항목(owner_contact_required 등). responsibility 사유에만 채워요.
    missing_contacts: list = field(default_factory=list)
    # 아직 통과하지 않은 게이트 단계 도구 id. gate 사유에만 채워요.
    incomplete_stages: list = field(default_factory=list)
    # 관측 시점의 게이트 verdict. 사유가 왜 그렇게 났는지 되짚는 근거예요.
    verdict: str = ""


@dataclass
class ToolVersion:
    """도구 버전/미러 이력 (§2.3 GOVTOOL#{id}/VERSION#{ts})."""
    version: str
    ts: str
    detected_at: str = ""
    test_result: str = ""     # "" | pass | fail
    promoted: bool = False    # active_version으로 승격됐나
