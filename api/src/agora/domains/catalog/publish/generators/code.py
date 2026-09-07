"""CodePluginGenerator — bundle을 plugin 파일트리로 렌더링(순수 함수).

산출: plugins/{slug}/ (repo 루트 기준). marketplace.json은 만들지 않아요 —
여러 bundle의 항목을 병합해야 해서 marketplace.py가 담당해요.
"""
from __future__ import annotations

import json

from ..naming import validate_plugin_name
from .common import ResolvedMember, slugify

# repo 루트 기준. managed 콘솔이 subdirectory path를 못 받아서 루트에 둬요.
PLUGIN_ROOT = "plugins"


def generate_code_plugin(
    bundle_name: str, bundle_desc: str, bundle_version: str,
    members: list[ResolvedMember], source_store,
) -> tuple[dict[str, bytes], dict]:
    """(파일트리, marketplace 항목) 2-튜플을 돌려줘요."""
    bundle_slug = validate_plugin_name(bundle_name)
    plugin_dir = f"{PLUGIN_ROOT}/{bundle_slug}"
    files: dict[str, bytes] = {}
    skill_rel_paths: list[str] = []
    mcp_servers: dict = {}

    for m in members:
        if m.asset_type == "skill":
            member_slug = slugify(m.name)
            for path in source_store.get_manifest(m.asset_id, m.version).paths():
                data = source_store.read_file(m.asset_id, m.version, path)
                files[f"{plugin_dir}/skills/{member_slug}/{path}"] = data
            skill_rel_paths.append(f"./skills/{member_slug}")
        elif m.asset_type == "mcp":
            endpoint = m.descriptors.get("mcp", {}).get("endpoint")
            if endpoint:
                mcp_servers[slugify(m.name)] = {"type": "http", "url": endpoint}

    plugin_json = {
        "name": bundle_slug,
        "description": bundle_desc,
        "version": bundle_version,
        "skills": skill_rel_paths,
    }
    files[f"{plugin_dir}/.claude-plugin/plugin.json"] = _json_bytes(plugin_json)

    if mcp_servers:
        files[f"{plugin_dir}/.mcp.json"] = _json_bytes({"mcpServers": mcp_servers})

    entry = {
        "name": bundle_slug,
        "source": f"./{PLUGIN_ROOT}/{bundle_slug}",
        "description": bundle_desc,
        "version": bundle_version,
    }
    return files, entry


def _json_bytes(obj: dict) -> bytes:
    return json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
