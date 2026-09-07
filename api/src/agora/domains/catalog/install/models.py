"""설치 명령 데이터 모델 — tool × asset_type × os 매트릭스의 출력."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class OS(str, Enum):
    MACOS = "macos"
    LINUX = "linux"
    WINDOWS = "windows"


class Shell(str, Enum):
    BASH = "bash"
    POWERSHELL = "powershell"


@dataclass(frozen=True)
class InstallInstruction:
    tool_id: str
    asset_type: str
    os: str
    shell: str
    kind: str                       # "shell" | "config"
    target_path: str
    note: str
    command: str | None = None
    config_snippet: dict | None = None
