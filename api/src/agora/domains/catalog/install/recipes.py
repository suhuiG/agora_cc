"""자산타입별 설치 recipe — DirectoryInstallRecipe(skill)·ConfigEntryRecipe(mcp)."""
from __future__ import annotations

import re
from dataclasses import dataclass

from .models import OS, Shell, InstallInstruction

_UNSAFE = re.compile(r"[^a-zA-Z0-9._-]+")


def safe_name(name: str) -> str:
    """설치 경로에 안전한 이름. 기존 web installCommand 규칙과 동일."""
    s = _UNSAFE.sub("-", name).strip("-")
    return s or "skill"


@dataclass(frozen=True)
class DirectoryInstallRecipe:
    """skill — 파일 트리를 OS별 경로에 tar로 푼다."""
    base_dir: dict[OS, str]

    def build(self, name: str, archive_url: str, os: OS) -> InstallInstruction:
        n = safe_name(name)
        base = self.base_dir[os]
        if os is OS.WINDOWS:
            target = rf"$env:USERPROFILE\.claude\skills\{n}"
            command = (
                f'New-Item -ItemType Directory -Force "{target}" | Out-Null; '
                f'curl.exe -fsSL "{archive_url}" | tar -xz -C "{target}"'
            )
            shell = Shell.POWERSHELL
            display_path = rf"{base}\{n}"
        else:
            target = f"{base}/{n}"
            command = (
                f'mkdir -p {target} && '
                f'curl -fsSL "{archive_url}" | tar -xz -C {target}'
            )
            shell = Shell.BASH
            display_path = target
        return InstallInstruction(
            tool_id="", asset_type="skill", os=os.value, shell=shell.value,
            kind="shell", command=command, target_path=display_path,
            note=f"이 명령을 터미널에 붙여넣으면 {display_path} 에 설치돼요.",
        )


@dataclass(frozen=True)
class ConfigEntryRecipe:
    """mcp — 확보된 endpoint를 tool 설정에 등록. endpoint 없으면 배포 대기 안내."""

    def build(self, name: str, endpoint: str | None, os: OS) -> InstallInstruction:
        n = safe_name(name)
        if not endpoint:
            return InstallInstruction(
                tool_id="", asset_type="mcp", os=os.value, shell=Shell.BASH.value,
                kind="config", command=None, config_snippet=None,
                target_path="~/.claude.json",
                note="아직 배포되지 않은 MCP예요. 배포가 완료되면 설치할 수 있어요.",
            )
        return InstallInstruction(
            tool_id="", asset_type="mcp", os=os.value, shell=Shell.BASH.value,
            kind="config",
            command=f"claude mcp add --transport http {n} {endpoint}",
            config_snippet={"mcpServers": {n: {"type": "http", "url": endpoint}}},
            target_path="~/.claude.json 또는 프로젝트 .mcp.json",
            note="CLI 명령을 실행하거나 .mcp.json에 snippet을 병합하세요.",
        )
