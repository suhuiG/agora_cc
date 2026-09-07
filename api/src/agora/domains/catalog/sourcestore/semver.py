"""semver(MAJOR.MINOR.PATCH) 파싱·검증·정렬키.

DDB 버전 SK를 `VER#{sort_key}` 로 저장해 사전식 정렬이 곧 버전 순서가 되게 해요.
prerelease/build 메타데이터는 MVP 범위 밖이라 받지 않아요 (X.Y.Z 숫자만).
"""
from __future__ import annotations

import re

_SEMVER_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
_PAD = 5  # 각 컴포넌트 자리수 (최대 99999)


def is_valid_semver(version: str) -> bool:
    return bool(_SEMVER_RE.match(version or ""))


def parse_semver(version: str) -> tuple[int, int, int]:
    m = _SEMVER_RE.match(version or "")
    if not m:
        raise ValueError(f"invalid semver: {version!r}")
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def sort_key(version: str) -> str:
    """정렬 가능한 zero-pad 키. 예: '1.2.3' -> '00001.00002.00003'."""
    major, minor, patch = parse_semver(version)
    return f"{major:0{_PAD}d}.{minor:0{_PAD}d}.{patch:0{_PAD}d}"
