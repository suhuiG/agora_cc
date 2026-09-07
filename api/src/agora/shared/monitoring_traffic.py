"""Cross-domain contract for independently observed agent invocation traffic."""

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class TrafficObservation:
    status: Literal["ok", "unknown"]
    invocation_count: int | None
    reason: str | None = None
    # 성공한 호출이 관측된 agent 들. "span 이 올 리 없는 트래픽"과 "적재가 멈춘
    # 트래픽"을 구분하려면 호출 주인의 계기 상태를 봐야 해요 — 호출 건수만으로
    # `ingest_stalled` 를 단정하면 계기 없는 agent 한 번 호출에도 파이프라인이
    # 죽었다고 말하게 돼요(2026-08-24 로컬 실AWS 실측).
    agent_ids: tuple[str, ...] = ()


# ── invocation_outcome 어휘의 단일 출처 ────────────────────────────────────
# 2026-08-24: 병렬 워커 두 명이 이 계약의 양쪽을 각자 구현하면서 쓰는 쪽은
# `"SUCCESS"`/`"FAILED"`, 읽는 쪽은 `"success"`/`"failure"` 를 썼어요. 값이
# 어긋나 실제 감사가 전부 `missing_outcome` 으로 떨어졌고, MO-36 의 idle↔stalled
# 판정이 프로덕션에서 한 번도 작동하지 않는 상태였어요. **양쪽 테스트가 각자
# 리터럴을 하드코딩해서 둘 다 통과했어요** — ADR-0037 §4 가 말하는
# "프로덕션에 배선되지 않은 검사는 검사가 아니다" 의 전형이에요.
#
# 그래서 어휘를 여기 한 곳에만 두고 writer(identity)·reader(monitoring)가 함께
# import 해요. 리터럴을 다시 코드에 박지 마세요.
INVOCATION_OUTCOME_SUCCESS = "SUCCESS"
INVOCATION_OUTCOME_FAILURE = "FAILED"
INVOCATION_OUTCOMES = frozenset(
    {INVOCATION_OUTCOME_SUCCESS, INVOCATION_OUTCOME_FAILURE}
)
