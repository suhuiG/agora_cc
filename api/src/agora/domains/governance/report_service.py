"""위협리포트.md 생성 (§2.1·§3-F).

스캔 findings를 사람이 읽고 고칠 수 있는 마크다운으로 렌더해요.
- BedrockReportGenerator: Bedrock Claude Sonnet 4.6(global)로 에셋별 맞춤 수정가이드 생성.
- TemplateReportGenerator: LLM 없이 결정론적 템플릿(폴백·테스트).
ReportService가 생성 입력(최신 스캔 + 자산 메타 + 목표 등급) 서명으로 캐싱하고,
LLM 실패 시 템플릿으로 폴백해요.

⚠️ 이 리포트는 **게이트 판정을 계산하지 않아요** — 그래서 게이트 수치를 «찍지도 않아요».
게이트 판정은 승인 큐 화면(`queue_router.get_gates` → `gate.compute_gates`)이 소유해요.
자세한 이유와 사고 기록은 `ReportGeneratorPort` docstring 에 있어요(IH-166).

[확장 지점] LLM 제공자 교체는 ReportGeneratorPort 구현 하나만 바꾸면 돼요.
finding code → OWASP/프레임워크 ID 매핑으로 설득력·감사성을 높여요(화면설계 §2.1).
"""
from __future__ import annotations

import json
from typing import Protocol, runtime_checkable

# finding code → (한글 위협명, 프레임워크 ID) 매핑. 리서치 §13 프레임워크 근거.
_CODE_META = {
    "SECRET_AWS_ACCESS_KEY": ("AWS 액세스 키 하드코딩", "OWASP LLM02 Sensitive Info"),
    "SECRET_HARDCODED": ("시크릿 하드코딩", "OWASP LLM02 Sensitive Info"),
    "SECRET_PRIVATE_KEY": ("개인키 노출", "OWASP LLM02 Sensitive Info"),
    "SAST_CURL_PIPE_SH": ("curl|sh 설치 유인", "OWASP LLM05 Improper Output"),
    "SAST_BASE64_EXEC": ("base64 난독화 실행", "OWASP LLM05 Improper Output"),
    "SAST_EVAL": ("eval 동적 실행", "OWASP LLM05 Improper Output"),
    "MCP_TOOL_POISONING": ("MCP tool poisoning", "OWASP LLM01 Prompt Injection"),
    "CROSS_TOOL_SHADOW": ("cross-tool shadowing", "OWASP LLM01 Prompt Injection"),
    "SCANNER_ERROR": ("스캐너 예외 (fail-closed)", "P1 §4"),
}

_TIER_LABEL = {"minimal": "최소", "standard": "표준", "strong": "강화"}

# 스캔 상태(`ScanRecord.status`) 라벨. 게이트 «판정»이 아니라 「스캔이 어디까지 갔나」예요 —
# 이 리포트가 실제로 관측하는 유일한 상태값이라, 여기만 값을 적을 수 있어요.
# "none" 은 `store.latest_scan` 이 None 을 준 경우예요. 그 스토어들은 조회 실패를 삼키지
# 않고 올려보내므로(`store.latest_scan` · `dynamo_store._query`) None 은 «관측된 부재»예요 —
# 미관측을 부재로 적는 게 아니에요(ADR-0111 결정 2).
_SCAN_STATUS_LABEL = {
    "done": "완료", "running": "진행 중", "failed": "실패", "none": "스캔 기록 없음",
}

# 게이트 판정을 «계산하지 않는다»는 사실을 그대로 적는 한 줄.
# 「0/0」·「없음」처럼 측정값으로 읽히는 표기를 쓰지 않는 것이 이 문장의 존재 이유예요.
_GATE_OUT_OF_SCOPE = "게이트 판정은 이 리포트의 범위가 아니에요 — 관리자 콘솔의 승인 큐에서 봐요."


@runtime_checkable
class ReportGeneratorPort(Protocol):
    """위협리포트 본문 생성기.

    ⚠️ `scan_summary` 는 **게이트 요약이 아니에요.** 이 리포트는 게이트를 계산하지 않아요 —
    `gate.compute_gates` 가 요구하는 것 중 `cells`·`tools` 를 `ReportService` 가 갖고 있지
    않거든요(`asset_type` 은 `get_report` 인자로 받고, `has_source`·`scan_applicability` 는
    기본값이 있어요). 그래서 실제로 관측한 값 하나(`status`)만 넘겨요 — `latest_scan()` 이
    레코드를 주면 `ScanRecord.status`(`queued`·`running`·`done`·`failed`) 이고, 레코드가
    없으면 이 모듈이 `none` 을 넣어요. `none` 은 상태값이 아니라 **「레코드 없음」 센티넬**이에요.
    같은 패키지의 `gate.gate_summary()` 가 주는 `{passed, total, verdict}` 와 **다른 물건**이니
    그 결과를 여기에 흘려보내지 마세요.

    ⚠️ 여기에 `passed`/`total` 같은 게이트 수치를 (다시) 넣지 마세요. 2026-09-05 까지
    `get_report` 가 `{"passed": 0, "total": 0}` 을 **하드코딩**해서 모든 자산의 리포트가
    「통과 게이트: 0/0」과 「미통과 항목을 해소한 뒤 재스캔하세요」를 찍었고, 같은 화면이 같은
    자산을 「4/4 통과 · 스캔 완료 · 승인」으로 보여줬어요(IH-166, 브라우저 실측). 계산하지
    않기로 한 값을 `0` 으로 접어 «측정값처럼» 찍는 건 ADR-0111 결정 2 가 금지하는 형태고,
    이 산출물은 고객·감사에게 나가요. 게이트 수치가 정말 필요하면 게이트 입력을 주입하는 별
    티켓이지, 이 Port 에 숫자를 되돌리는 일이 아니에요.
    """

    def generate(
        self, *, asset_name: str, asset_type: str, version: str, tier: str,
        risk: str, findings: list, scan_summary: dict,
    ) -> str: ...


def _scan_status_ko(scan_summary: dict) -> str:
    """`scan_summary["status"]` 를 사람이 읽는 라벨로. 모르는 값은 원문 그대로 둬요."""
    status = str(scan_summary.get("status") or "none")
    return _SCAN_STATUS_LABEL.get(status, status)


def _code_meta(code: str) -> tuple[str, str]:
    return _CODE_META.get(code, (code, ""))


class TemplateReportGenerator:
    """LLM 없이 결정론적 마크다운 렌더 (폴백·테스트용)."""

    def generate(
        self, *, asset_name: str, asset_type: str, version: str, tier: str,
        risk: str, findings: list, scan_summary: dict,
    ) -> str:
        tier_ko = _TIER_LABEL.get(tier, tier)
        lines = [
            f"# 거버넌스 스캔 리포트 — {asset_name} ({asset_type} · {version})",
            "",
            f"- 목표 등급: {tier_ko}  ·  위험도: {(risk or 'none').upper()}"
            f"  ·  스캔 상태: {_scan_status_ko(scan_summary)}",
            f"- {_GATE_OUT_OF_SCOPE}",
            "",
            "## 검출 위협",
            "",
        ]
        if not findings:
            lines.append("검출된 위협이 없어요. 목표 등급의 게이트를 통과하면 승인 대상이에요.")
            lines.append("")   # 다음 제목 앞 빈 줄(마크다운 렌더러가 제목을 인식해야 해요)
        else:
            for f in findings:
                code = str(f.get("code", "UNKNOWN"))
                name, fw = _code_meta(code)
                sev = str(f.get("severity", "")).upper()
                lines.append(f"### [{sev}] {code} — {name}")
                if f.get("location"):
                    lines.append(f"- 위치: {f['location']}")
                if f.get("detail"):
                    lines.append(f"- 내용: {f['detail']}")
                if fw:
                    lines.append(f"- 위협 근거: {fw}")
                lines.append(f"- **수정 가이드**: {_generic_fix(code)}")
                lines.append("")
        # 「미충족 게이트」 섹션은 뺐어요 — 이 생성기는 어느 게이트가 미통과인지 모르는데
        # 「통과 0/0. 미통과 항목을 해소한 뒤 재스캔하세요」를 찍고 있었어요(IH-166).
        # 모르는 것은 머리줄의 범위 고지(`_GATE_OUT_OF_SCOPE`)로 대신 말해요.
        lines += [
            "## 다음 단계",
            "수정 후 재등록하면 스캔이 다시 돌아요. 게이트 통과 여부는 관리자 콘솔의 승인 큐에서 "
            "확인해요. 문의: governance 채널.",
        ]
        return "\n".join(lines)


def _generic_fix(code: str) -> str:
    if code.startswith("SECRET"):
        return "하드코딩된 시크릿을 제거하고 환경변수·시크릿 매니저로 이전한 뒤 노출된 키를 폐기·회전하세요."
    if code.startswith("SAST"):
        return "설치 유인·난독화 실행 패턴을 제거하고, 필요한 명령은 검증된 패키지로 대체하세요."
    if code.startswith("MCP") or code.startswith("CROSS"):
        return "tool description에서 지시문·명령형 문구를 제거하고 순수 기능 설명만 남긴 뒤 재스캔하세요."
    return "검출 항목을 검토·수정한 뒤 재스캔하세요."


class BedrockReportGenerator:
    """Bedrock Claude Sonnet 4.6(global)로 에셋별 맞춤 수정가이드 생성.

    boto3 bedrock-runtime.invoke_model 직접 호출 (새 SDK 의존성 없음).
    모델 ID·리전은 env로 조정: AGORA_REPORT_MODEL / AGORA_REPORT_REGION.
    """

    def __init__(self, model_id: str, region: str, client=None):
        self._model_id = model_id
        self._region = region
        self._client = client  # 테스트 주입용; 없으면 지연 생성

    def _rt(self):
        if self._client is None:
            import boto3
            self._client = boto3.client("bedrock-runtime", region_name=self._region)
        return self._client

    def generate(
        self, *, asset_name: str, asset_type: str, version: str, tier: str,
        risk: str, findings: list, scan_summary: dict,
    ) -> str:
        tier_ko = _TIER_LABEL.get(tier, tier)
        # findings를 프레임워크 근거와 함께 프롬프트에 담아요.
        findings_desc = json.dumps(
            [{**f, "framework": _code_meta(str(f.get("code", "")))[1]} for f in findings],
            ensure_ascii=False, indent=2,
        )
        # 지침과 입력이 모순이면 모델이 지어내요. 예전 프롬프트는 「추측성 내용은 넣지 말고,
        # 주어진 findings에만 근거해요」라고 하면서 「미충족 게이트」 섹션을 요구했고, 게이트
        # 정보로는 하드코딩된 `0/0` 을 먹였어요 — 그래서 모든 리포트가 「미통과 항목을 해소한
        # 뒤 재스캔하세요」로 끝났어요(IH-166). 이제 그 수치를 주지 않고, 대신 게이트가 이
        # 리포트의 범위가 아니라는 사실을 지침으로 못박아요.
        system = (
            "당신은 사내 거버넌스 보안 리뷰어예요. 스캔 결과를 바탕으로 자산 제출자가 "
            "바로 고칠 수 있는 위협리포트를 한국어 마크다운으로 작성해요. 각 위협에 대해 "
            "이 자산에 특정된 구체적 수정 단계를 제시하고, OWASP LLM 등 프레임워크 근거를 병기해요. "
            "추측성 내용은 넣지 말고, 주어진 findings에만 근거해요. 마크다운만 출력해요.\n"
            "게이트 판정은 입력에 들어 있지 않아요. 그러니 게이트 판정·개수·비율이나 "
            "미해소 항목 목록을 지어내지 말고, 그 자리에는 다음 한 줄만 그대로 써요: "
            f"{_GATE_OUT_OF_SCOPE}"
        )
        user = (
            f"자산: {asset_name} ({asset_type} · {version})\n"
            f"목표 등급: {tier_ko}  ·  위험도: {(risk or 'none').upper()}  ·  "
            f"스캔 상태: {_scan_status_ko(scan_summary)}\n\n"
            f"검출 findings (JSON):\n{findings_desc}\n\n"
            "위 findings로 위협리포트.md를 작성해줘. 섹션: 제목 / 검출 위협(각 위협별 위치·근거·"
            "이 자산 맞춤 수정 가이드) / 다음 단계. findings가 비어 있으면 위협 없음을 명시해."
        )
        body = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 4000,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        resp = self._rt().invoke_model(modelId=self._model_id, body=json.dumps(body))
        out = json.loads(resp["body"].read())
        text = "".join(b.get("text", "") for b in out.get("content", []) if b.get("type") == "text")
        if not text.strip():
            raise RuntimeError("빈 LLM 응답")
        return text


class ReportService:
    """생성 입력 전체를 서명으로 캐싱 + LLM 실패 시 폴백.

    캐시는 `generate(**kwargs)` 의 메모예요 — 그래서 **생성기에 들어가는 모든 입력**이
    서명에 들어가야 해요. 서명이 입력 하나를 빠뜨리면 그 입력을 바꾸는 쓰기가 조용히
    no-op 이 되거든요. 실제로 그렇게 됐어요(IH-166 F3, 2026-09-05 실측): 서명이
    `scan_ts` 뿐이라 `PATCH /api/governance/queue/{id}/tier` 로 라벨을 바꿔도 다음
    `report.md` 가 **바이트가 같은** 옛 문서였고, 같은 화면이 「이 값은 위협리포트
    라벨에만 쓰여요」를 단정하고 있었어요. 새 입력을 generate 에 넘기게 되면 `_signature`
    에도 같이 넣어야 해요.

    dict 키는 계속 `record_id` 하나예요(서명은 값 쪽에 둬요) — 서명을 키로 쓰면 재스캔·
    라벨 변경마다 항목이 영구히 쌓여 장수 프로세스에서 새요. 지금 형태는 record 당 항목이
    하나라 입력이 바뀌면 그 항목을 «교체» 해요.

    캐시는 프로세스 메모리예요. 그래도 tier 는 매 요청 공유 스토어에서 다시 읽어
    (`queue_router.threat_report` → `store.get_target_tier`) 서명에 들어가므로, PATCH 를
    받지 않은 다른 태스크도 «영구히» 옛 라벨에 갇히지 않아요 — 그 프로세스도 새 tier 로
    서명을 만들어 캐시 미스가 나요. 이게 `invalidate()` 를 열어 `set_tier` 에서 부르는
    대안보다 나은 점이에요(무효화는 PATCH 를 받은 그 프로세스만 지워요).

    ⚠️ 그래도 **즉시성은 보장하지 않아요.** `DynamoGovStore.get_target_tier` 는
    `ConsistentRead` 없는 `get_item` 이라, 쓰기 직후 좁은 창에서 옛 tier 를 읽어 옛 문서를
    한 번 더 내줄 수 있어요(다음 요청에서 스스로 회복해요). 관측하지 않은 창이에요.
    """

    def __init__(self, generator: ReportGeneratorPort, store, fallback=None):
        self._gen = generator
        self._store = store
        self._fallback = fallback
        # record_id -> (생성 입력 서명, md)
        self._cache: dict[str, tuple[tuple[str, ...], str]] = {}

    @staticmethod
    def _signature(
        *, scan_ts: str, asset_name: str, asset_type: str, version: str, tier: str,
    ) -> tuple[str, ...]:
        """생성 입력 서명. 하나라도 바뀌면 캐시 미스가 나야 해요.

        스캔에서 오는 입력(`risk`·`findings`·`status`)은 `scan_ts` **하나로 대표해요.**
        그게 참인 근거는 스캔 레코드가 «제자리 변경되지 않는다» 는 것이에요 — 쓰는 쪽
        (`scan_service` · `stepfn_runner`. `overlap_review` 는 `ScanRecord` 가 아니라
        `OverlapRecord` 를 써서 이 축이 아니에요)이 전부 `ts` 를 새로 찍어
        레코드를 통째로 넣고, running→done 전이도 새 `ts` 를 받아요
        (`stepfn_runner.sync_execution` 의 `ts=now`). 상태만 갱신하는 경로를 새로 만들면
        이 대표가 깨지므로 그때는 `status` 를 서명에 직접 넣어야 해요. 세어서 확인하려면
        `ScanRecord(` 호출부를 grep 하세요 — 개수는 여기 적지 않아요.
        """
        return (scan_ts, tier, asset_name, asset_type, version)

    def get_report(
        self, record_id: str, *, asset_name: str, asset_type: str, version: str, tier: str,
    ) -> str:
        scan = self._store.latest_scan(record_id)
        scan_ts = scan.ts if scan else "none"
        signature = self._signature(
            scan_ts=scan_ts, asset_name=asset_name, asset_type=asset_type,
            version=version, tier=tier,
        )
        cached = self._cache.get(record_id)
        if cached and cached[0] == signature:
            return cached[1]

        risk = scan.risk if scan else "none"
        findings = scan.findings if scan else []
        # 게이트 판정은 «계산하지 않아요». `gate.compute_gates` 가 요구하는 cells·tools 를
        # 이 서비스가 갖고 있지 않거든요(asset_type 은 인자로 받아요).
        # 그래서 관측한 값(스캔 상태)만 넘기고 `passed`/`total` 은 **만들지 않아요** —
        # 예전엔 `{"passed": 0, "total": 0}` 을 하드코딩해 「통과 게이트: 0/0」을 측정값처럼
        # 찍었어요(IH-166 · ADR-0111 결정 2: 미관측은 부재가 아니에요).
        scan_summary = {"status": scan.status if scan else "none"}
        kwargs = dict(
            asset_name=asset_name, asset_type=asset_type, version=version,
            tier=tier, risk=risk, findings=findings, scan_summary=scan_summary,
        )
        try:
            md = self._gen.generate(**kwargs)
        except Exception:
            if self._fallback is None:
                raise
            md = self._fallback.generate(**kwargs)
        self._cache[record_id] = (signature, md)
        return md
