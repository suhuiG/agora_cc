"""ScannerPort + StaticScanner (최소 등급 실스캔).

P1: 외부 바이너리 없이 순수 파이썬 정규식으로 시크릿·설치유인 패턴 검사(로컬 실동작).
인프라 phase에서 Gitleaks·Semgrep 바이너리를 scan-runner에서 실행하는 어댑터로 승격.
스캐너 예외는 risk=high로 fail-closed.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Protocol, runtime_checkable


class ScanRisk(str, Enum):
    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


_ORDER = {"none": 0, "low": 1, "medium": 2, "high": 3}


@dataclass
class Finding:
    code: str
    severity: str
    detail: str
    location: str = ""


@dataclass
class ScanOutcome:
    risk: str
    findings: list = field(default_factory=list)
    # 이 스캔이 실제로 검사한 도구 area 목록. 게이트가 "미실행"과 "검사 후 통과"를
    # 구분하는 근거예요(비어 있으면 하위호환: 기존 pass 판정).
    scanned_areas: list = field(default_factory=list)
    # 스캐너 종류(ScanRecord.scanner_kind와 대칭). ""=레거시(gate가 scanned_areas 비어도 pass
    # 하위호환), "static"/"stepfn"/"fargate"=엄격 판정(scanned_areas 비면 not_run→pending, 가짜 승인 차단).
    scanner_kind: str = ""
    # True = 비동기 스캔 시작만 됨(결과 미확정). run()이 done 대신 running 유지.
    pending: bool = False


@runtime_checkable
class ScannerPort(Protocol):
    def scan(self, asset_type: str, descriptors: dict, source_files) -> ScanOutcome: ...


class NoopScanner:
    def scan(self, asset_type: str, descriptors: dict, source_files) -> ScanOutcome:
        return ScanOutcome(risk="none", findings=[])


# Gitleaks류 시크릿 패턴 (대표 몇 개) / Semgrep류 설치유인 패턴
_SECRET_PATTERNS = [
    (r"AKIA[0-9A-Z]{16}", "SECRET_AWS_ACCESS_KEY", "high"),
    (r"(?i)(secret|password|api[_-]?key)\s*=\s*['\"][^'\"]{8,}", "SECRET_HARDCODED", "high"),
    (r"-----BEGIN (RSA |EC )?PRIVATE KEY-----", "SECRET_PRIVATE_KEY", "high"),
]
_INSTALL_LURE = [
    (r"curl\s+[^\n|]*\|\s*(sudo\s+)?sh", "SAST_CURL_PIPE_SH", "high"),
    (r"base64\s+-d[^\n]*\|\s*(ba)?sh", "SAST_BASE64_EXEC", "high"),
    (r"(?i)eval\s*\(", "SAST_EVAL", "medium"),
]


class StaticScanner:
    def scan(self, asset_type: str, descriptors: dict, source_files) -> ScanOutcome:
        try:
            files = source_files or []
            findings: list[Finding] = []
            for path, content in files:
                text = content.decode("utf-8", errors="replace")  # None이면 AttributeError → fail-closed
                for pat, code, sev in _SECRET_PATTERNS + _INSTALL_LURE:
                    if re.search(pat, text):
                        findings.append(Finding(code=code, severity=sev,
                                                detail=f"패턴 매치: {code}", location=path))
            risk = "none"
            for f in findings:
                if _ORDER[f.severity] > _ORDER[risk]:
                    risk = f.severity
            # scanned_areas는 "실제로 검사한 area". 소스 파일이 하나라도 있으면 secret·sast 패턴을
            # 모두 돌렸으니 두 area를 검사한 것. 소스가 0개면(예: 소스 비관리 MCP) for 루프를 한 번도
            # 안 돌아 아무것도 검사 안 했으므로 빈 리스트(빈 검사를 정직하게 표기).
            scanned_areas = ["secret", "sast"] if files else []
            # scanner_kind="static": gate.py의 strict 방어를 받아, 소스 없는 빈 검사(scanned_areas=[])는
            # not_run→verdict pending으로 처리돼 가짜 auto-approve를 차단해요(레거시 ""는 pass로 샜음).
            return ScanOutcome(risk=risk, findings=[asdict(f) for f in findings],
                               scanned_areas=scanned_areas, scanner_kind="static")
        except Exception:
            # fail-closed. 에러라 실제 검사한 area 없음(scanned_areas 빈) + scanner_kind="static" 유지.
            return ScanOutcome(risk="high", findings=[
                asdict(Finding(code="SCANNER_ERROR", severity="high", detail="스캐너 예외 — fail-closed"))
            ], scanner_kind="static")
