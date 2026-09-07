"""거버넌스 등급 매트릭스 시드 (화면설계 §4-A·리서치 §14.1).

도구 목록·메타는 tool_catalog.TOOL_CATALOG(코드 상수)가 정본이에요. 여기서는
등급 매트릭스(tier×tool enforcement)만 시드해요 — 이건 admin이 UI(tiers 탭)로
런타임 편집하는 운영 정책이라 GovStore에 남겨요.

seed_tiers는 reconcile(upsert)예요: 코드 _TIER_MATRIX에 정의됐지만 스토어에 없는
(tier, tool) cell을 매 기동 시 보강해요. 이미 있는 cell의 enforcement는 보존해
admin UI 편집을 존중해요. (신규 도구 추가 시 옛 스토어가 stale해지는 문제 방지 —
2026-07-18 통합테스트에서 llm-judge가 옛 시드에 없어 SFN 미투입되던 결함 수정.)

하위호환 승격: 기존 스토어에 recommend 값이 있으면 warn으로 승격해요. enforcement는
required/warn/off 3종으로 축소됐고(2026-07-28), recommend는 더 이상 유효한 값이
아니에요. warn/required/off는 건드리지 않아요(admin UI 편집 존중).
"""
from __future__ import annotations

from .models import TierCell

# 등급 매트릭스 초기/기본 시드 (최소/표준/강화 × 도구).
#
# 원칙: air-gap 스캔 파이프라인에서 실제 배포·실행 가능한 도구만 참조.
#   gitleaks(secret)·semgrep(sast) — offline 바이너리.
#   trivy(sbom_cve)  — 취약점 DB 번들로 offline 동작.
#   llm-judge(agent_intent) — Bedrock Converse(agent 고유 위협: prompt injection·tool poisoning).
#     agent(strong)만 required로 게이트에 편입, skill/mcp(minimal/standard)는 warn(관측·기록).
#   [제외] presidio·snyk-agent-scan·garak·cosign — tool_catalog에서도 제거됨(미배포·미사용).
#
#   최소   : 시크릿·SAST 필수 + llm-judge 경고 (skill)
#   표준   : + SBOM/CVE 필수 + llm-judge 경고 (mcp)
#   강화   : 시크릿·SAST·SBOM/CVE 전부 필수 + llm-judge 필수 (agent 고유 위협 게이트)
_TIER_MATRIX = {
    "minimal": {"gitleaks": "required", "semgrep": "required", "llm-judge": "warn"},
    "standard": {
        "gitleaks": "required", "semgrep": "required",
        "trivy": "required",
        "llm-judge": "warn",
    },
    "strong": {
        "gitleaks": "required", "semgrep": "required",
        "trivy": "required",
        "llm-judge": "required",
    },
}


def seed_tiers(store) -> None:
    """코드 _TIER_MATRIX를 스토어에 reconcile(upsert)해요.

    - 스토어에 없는 (tier, tool) cell → 코드 기본 등급으로 추가.
    - 이미 있는 cell → enforcement 보존(admin UI 편집 존중, 덮어쓰지 않음).
      단, enforcement == "recommend"인 cell은 warn으로 승격해요
      (enforcement 3종 축소 하위호환, 2026-07-28).
    - 멱등: 반복 호출해도 중복 cell이 생기지 않아요.
    """
    for tier, code_cells in _TIER_MATRIX.items():
        existing = {c.tool_id: c for c in store.get_tier(tier)}
        changed = False

        # 1단계: recommend 셀 승격 (warn/required/off는 절대 건드리지 않음)
        for cell in existing.values():
            if cell.enforcement == "recommend":
                cell.enforcement = "warn"
                changed = True

        # 2단계: 스토어에 없는 cell 보강
        for tool_id, enf in code_cells.items():
            if tool_id not in existing:
                existing[tool_id] = TierCell(tier=tier, tool_id=tool_id, enforcement=enf)
                changed = True

        if changed:
            store.put_tier(tier, list(existing.values()))
