#!/usr/bin/env python3
"""포털 배포 preflight — 합성 결과를 **올바른 스택에서** 검사해요.

`deploy-portal.sh` 가 호출해요. 단독 실행하지 마세요(env 로 합성 파일 경로를 받아요).

## 왜 양성 대조가 있나

문자열 부재로만 검사하면 **합성이 비었을 때 모든 항목이 통과로 읽혀요.** 2026-08-29 에
실제로 그렇게 0바이트 합성을 "하드룰 전부 통과" 로 읽었어요. 그래서 각 스택마다 "반드시
있어야 하는 문자열" 을 먼저 확인하고, 그게 없으면 나머지 검사를 아예 신뢰하지 않아요.

## 왜 스택을 나눠 보나

`AGORA_PORTAL_ORIGIN` 기반 S3 CORS 와 `agent-registry` 쓰기 권한은 **CatalogStorageStack**
소속이에요. PortalStack 에서 찾으면 "부재" 가 나오고, 그걸 결함으로 오진해요(같은 날 실측).
"""

from __future__ import annotations

import os
import re
import sys

FAILED: list[str] = []


def check(cond: object, label: str, detail: str = "") -> None:
    mark = "✓" if cond else "✖"
    print(f"  {mark} {label}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILED.append(label)


def missing_denied_actions(raw: str, actions: list[str]) -> list[str]:
    """합성 결과에서 빠진 Deny action 을 돌려줘요 (순서 유지).

    **경계 인식**이 핵심이에요. `GetWorkloadAccessToken` 은
    `GetWorkloadAccessTokenForUserId`·`...ForJWT` 의 substring 이라, 단순
    `name in raw` 로 검사하면 standalone action 이 사라져도 더 긴 형제가 남아 있는 한
    거짓 통과해요. 그래서 `bedrock-agentcore:<action>` 뒤에 식별자 문자(영숫자·`-`)가
    오지 않는 자리만 매칭해요. 접두어 규약(`bedrock-agentcore:`)은 라이브·합성 정책이
    쓰는 형태 그대로예요.
    """
    missing: list[str] = []
    for action in actions:
        pattern = r"bedrock-agentcore:" + re.escape(action) + r"(?![0-9A-Za-z-])"
        if not re.search(pattern, raw):
            missing.append(action)
    return missing


def env_value(raw: str, name: str) -> str | None:
    """합성 결과에서 env 항목의 Value 를 뽑아요 (YAML·JSON 양쪽 관용)."""
    m = re.search(r"Name:\s*" + re.escape(name) + r"\s*\n\s*Value:\s*(\S.*)", raw)
    if m:
        return m.group(1).strip().strip("\"'")
    m = re.search(
        r'"Name":\s*"' + re.escape(name) + r'",\s*"Value":\s*"([^"]*)"', raw
    )
    return m.group(1) if m else None


def main() -> int:
    portal_path = os.environ["PORTAL_SYNTH"]
    cat_path = os.environ["CAT_SYNTH"]
    portal_origin = os.environ["PORTAL_ORIGIN"]

    portal = open(portal_path).read()
    cat = open(cat_path).read()

    print("\n[PortalStack]")
    # 양성 대조 — 이게 실패하면 아래 결과를 신뢰하지 않아요.
    check("AWS::ECS::TaskDefinition" in portal, "[양성대조] ECS TaskDefinition 존재")
    check("Environment" in portal, "[양성대조] 환경변수 블록 존재")
    if FAILED:
        print("  ✖ 양성 대조 실패 — 합성 결과가 기대한 스택이 아니에요. 검사 중단.")
        return 1

    for name, want in (
        ("AGORA_ROLE", "portal"),
        ("AGORA_POLLER_ENABLED", "1"),
        ("AGORA_AUTH_MODE", "cognito"),
    ):
        got = env_value(portal, name)
        check(got == want, f"{name} = {want}", f"실제 {got!r}")

    # 웹 전용 키가 백엔드로 누출되면 안 돼요.
    check(
        "AGORA_DEMO_PASSWORD" not in portal,
        "데모 비밀번호가 합성에 없음",
    )

    print("\n[PortalStack · 하드룰]")
    princ = re.findall(r'Principal:\s*\n?\s*(?:AWS:\s*)?["\']?\*["\']?', portal)
    check(not princ, "Principal '*' 부재", f"발견 {len(princ)}")
    check("AuthType: NONE" not in portal, "Function URL AuthType=NONE 부재")
    check(":role/*" not in portal, "role/* 광역 grant 부재")
    check(
        "AgoraGovernanceScan-dev" not in portal,
        "미배포 GovernanceScan 참조 부재",
    )

    print("\n[CatalogStorageStack]")
    check("AWS::S3::Bucket" in cat, "[양성대조] S3 버킷 존재")
    if "AWS::S3::Bucket" not in cat:
        print("  ✖ 양성 대조 실패 — 검사 중단.")
        return 1

    # CORS — CloudFront origin 이 빠지면 소스 업로드가 깨져요.
    host = portal_origin.replace("https://", "")
    check(host in cat, "S3 CORS 에 CloudFront origin 포함", host)

    acts = [
        "ListRegistries", "CreateRegistry", "CreateRegistryRecord",
        "UpdateRegistryRecord", "UpdateRegistryRecordStatus",
        "GetRegistryRecord", "ListRegistryRecords", "DeleteRegistryRecord",
    ]
    missing = [a for a in acts if a not in cat]
    check(not missing, "agent-registry 쓰기 action 8개", f"누락 {missing}" if missing else "")

    # IA-57 — 포털 role 이 gateway control-plane 을 못 만지게 하는 명시 Deny.
    # sid AgoraPortalDenyGatewayControlPlane (catalog-storage-stack.ts:239-248).
    ia57_denies = [
        "UpdateGateway", "CreateGateway", "DeleteGateway",
        "UpdatePolicyEngine", "DeletePolicyEngine",
    ]
    has_deny = "Effect: Deny" in cat or '"Effect": "Deny"' in cat
    check(has_deny, "Deny 문 존재 (IA-57)")
    missing_ia57 = missing_denied_actions(cat, ia57_denies)
    check(
        not missing_ia57,
        "Deny 대상 action 5개 (IA-57 gateway control-plane)",
        f"누락 {missing_ia57}" if missing_ia57 else "",
    )

    # IA-76 — 포털 role 이 사람별 Token Vault 값을 못 읽게 하는 명시 Deny.
    # sid AgoraPortalDenyUserScopedTokenVault (catalog-storage-stack.ts:218-229).
    # IA-57 과 **별도 check** 예요 — 한 묶음으로 합치면 어느 축이 빠졌는지 안 보여요.
    # 기대값은 그 소스 파일이 소유하지만 여기 리터럴로 적어요: preflight 는 라이브 정책을
    # 읽어 대조하는 쪽이라, 소스에서 뽑으면 소스 대 소스 비교(자기검증)가 돼요.
    ia76_denies = [
        "GetWorkloadAccessTokenForUserId",
        "GetWorkloadAccessTokenForJWT",
        "GetWorkloadAccessToken",
        "GetResourceOauth2Token",
        "GetResourceApiKey",
    ]
    missing_ia76 = missing_denied_actions(cat, ia76_denies)
    check(
        not missing_ia76,
        "Deny 대상 action 5개 (IA-76 user-scoped Token Vault 읽기)",
        f"누락 {missing_ia76}" if missing_ia76 else "",
    )

    print()
    if FAILED:
        print(f"✖ preflight 실패 {len(FAILED)}건: {', '.join(FAILED)}")
        return 1
    print("✓ preflight 전부 통과")
    return 0


if __name__ == "__main__":
    sys.exit(main())
