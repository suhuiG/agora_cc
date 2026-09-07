"""설치 타깃 레지스트리 — 새 tool 추가는 여기 항목 1개."""
from __future__ import annotations

from dataclasses import dataclass

from .models import OS
from .recipes import ConfigEntryRecipe, DirectoryInstallRecipe


@dataclass(frozen=True)
class InstallTarget:
    tool_id: str
    display_name: str
    skill: DirectoryInstallRecipe | None
    mcp: ConfigEntryRecipe | None

    def recipe_for(self, asset_type: str):
        return getattr(self, asset_type, None)


INSTALL_TARGETS: dict[str, InstallTarget] = {
    "claude": InstallTarget(
        tool_id="claude",
        display_name="Claude Code",
        skill=DirectoryInstallRecipe(base_dir={
            OS.MACOS: "~/.claude/skills",
            OS.LINUX: "~/.claude/skills",
            OS.WINDOWS: r"%USERPROFILE%\.claude\skills",
        }),
        mcp=ConfigEntryRecipe(),
    ),
    # 미래: "codex": InstallTarget(...), "kiro": InstallTarget(...)
}


def get_target(tool_id: str) -> InstallTarget:
    return INSTALL_TARGETS[tool_id]


def targets_supporting(asset_type: str) -> list[InstallTarget]:
    return [t for t in INSTALL_TARGETS.values() if t.recipe_for(asset_type)]
