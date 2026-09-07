"""DesktopOrgPluginGenerator — bundle을 Claude Desktop 3P org-plugins로 렌더링(순수 함수).

산출: ClaudeDesktop/org-plugins/{bundle}/ (plugin.json·version.json·skills·.mcp.json).
3P 스키마 준수. stdio MCP 제외(endpoint 있는 원격만).
"""
from __future__ import annotations

import json

from .common import ResolvedMember, slugify

# 3P(MDM) 배포판 전용 폴백 경로. managed 콘솔은 이 트리를 쓰지 않아요.
DESKTOP_ROOT = "ClaudeDesktop/org-plugins"


def generate_desktop_org_plugin(
    bundle_name: str, bundle_desc: str, bundle_version: str,
    members: list[ResolvedMember], source_store,
) -> dict[str, bytes]:
    bundle_slug = slugify(bundle_name)
    plugin_dir = f"{DESKTOP_ROOT}/{bundle_slug}"
    files: dict[str, bytes] = {}
    mcp_servers: dict = {}

    for m in members:
        if m.asset_type == "skill":
            member_slug = slugify(m.name)
            for path in source_store.get_manifest(m.asset_id, m.version).paths():
                data = source_store.read_file(m.asset_id, m.version, path)
                files[f"{plugin_dir}/skills/{member_slug}/{path}"] = data
        elif m.asset_type == "mcp":
            endpoint = m.descriptors.get("mcp", {}).get("endpoint")
            if endpoint:
                mcp_servers[slugify(m.name)] = {"type": "http", "url": endpoint}

    plugin_json = {
        "name": bundle_slug,
        "description": bundle_desc,
        "version": bundle_version,
        "installationPreference": "available",
    }
    files[f"{plugin_dir}/.claude-plugin/plugin.json"] = _json_bytes(plugin_json)
    files[f"{plugin_dir}/version.json"] = _json_bytes({"version": bundle_version})

    if mcp_servers:
        files[f"{plugin_dir}/.mcp.json"] = _json_bytes({"mcpServers": mcp_servers})
    return files


def _json_bytes(obj: dict) -> bytes:
    return json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
