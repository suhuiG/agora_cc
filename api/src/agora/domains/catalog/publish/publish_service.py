"""PublishService — bundle을 서피스별 plugin으로 렌더링해 사용자 repo에 배포.

member를 registry로 확장(APPROVED만), source_store에서 파일을 읽어 생성기로 파일트리를
만들고, version을 patch bump한 뒤 배포해요. marketplace.json은 기존 항목을 보존하며
병합해요(managed sync가 repo 상태로 전체 교체하므로 덮어쓰면 다른 plugin이 사라져요).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..registry.models import DescriptorType, RecordStatus
from .generators.code import PLUGIN_ROOT, generate_code_plugin
from .generators.common import ResolvedMember, parse_source_prefix, slugify
from .generators.desktop import DESKTOP_ROOT, generate_desktop_org_plugin
from .generators.marketplace import (
    LEGACY_MARKETPLACE_PATH, MARKETPLACE_EMPTY, MARKETPLACE_PATH,
    merge_marketplace, prune_marketplace,
)

_ASSET_TYPE_BY_DESCRIPTOR = {
    DescriptorType.SKILL: "skill",
    DescriptorType.MCP: "mcp",
    DescriptorType.AGENT: "agent",
}

# marketplace.json의 최상위 이름·소유자. 조직당 repo 1개라 고정이에요.
_MARKETPLACE_NAME = "agora"
_MARKETPLACE_OWNER = "Agora"

# M5에서 쓰던 옛 plugin 경로 — 마이그레이션 정리 대상.
_LEGACY_PLUGIN_ROOT = "ClaudeCode/plugins"

# managed 조직 콘솔. Agora는 PR까지만 하고 등록·강제수준은 관리자가 여기서 해요.
_CONSOLE_URL = "https://claude.ai/admin-settings/plugins"


def bump_patch(version: str) -> str:
    if not version:
        return "0.1.0"
    parts = version.split(".")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        return "0.1.0"
    major, minor, patch = (int(p) for p in parts)
    return f"{major}.{minor}.{patch + 1}"


@dataclass
class PublishResult:
    commit_sha: str
    version: str
    surfaces: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    files_written: int = 0
    pr_url: str = ""


class PublishService:
    def __init__(self, *, registry, registry_id, source_store,
                 connection_service, publisher, now) -> None:
        self.registry = registry
        self.registry_id = registry_id
        self.source_store = source_store
        self.connection_service = connection_service
        self.publisher = publisher
        self.now = now

    def _resolve_members(self, bundle) -> tuple[list[ResolvedMember], list[str]]:
        members: list[ResolvedMember] = []
        skipped: list[str] = []
        for rid in bundle.member_ids:
            rec = self.registry.get_record(self.registry_id, rid)
            if rec.status != RecordStatus.APPROVED:
                skipped.append(rid)
                continue
            asset_type = _ASSET_TYPE_BY_DESCRIPTOR.get(rec.descriptor_type)
            if asset_type is None:
                skipped.append(rid)
                continue
            asset_id, version = ("", "")
            if rec.source_prefix:
                asset_id, version = parse_source_prefix(rec.source_prefix)
            members.append(ResolvedMember(
                record_id=rid, name=rec.name, asset_type=asset_type,
                version=version, asset_id=asset_id, descriptors=rec.descriptors))
        return members, skipped

    def publish(self, bundle, *, mode: str = "managed") -> PublishResult:
        """bundle을 렌더링해 배포해요.

        mode="managed"(기본)는 PR을 열어요 — 조직 콘솔의 자동 sync가 version bump를
        포함한 PR 머지에만 걸리고 직접 push는 트리거되지 않아요. mode="3p"는 sync
        개념이 없어서 default branch로 바로 push해요.
        """
        conn = self.connection_service.get()
        if conn is None:
            raise LookupError("연결된 repo가 없어요")
        token = self.connection_service.token_for(conn)

        members, skipped = self._resolve_members(bundle)
        version = bump_patch(bundle.last_published_version)

        files: dict[str, bytes] = {}
        surfaces: list[str] = []
        entry: dict | None = None
        if "claude-code" in bundle.surfaces:
            code_files, entry = generate_code_plugin(
                bundle.name, bundle.description, version, members, self.source_store)
            files.update(code_files)
            surfaces.append("claude-code")
        if "claude-desktop" in bundle.surfaces:
            files.update(generate_desktop_org_plugin(
                bundle.name, bundle.description, version, members, self.source_store))
            surfaces.append("claude-desktop")

        # marketplace.json 병합 — 기존 항목 보존. 파싱 실패면 ValueError로 중단해요.
        if entry is not None:
            existing = self.publisher.read_file(conn, MARKETPLACE_PATH, token=token)
            files[MARKETPLACE_PATH] = merge_marketplace(
                existing, entry, marketplace_name=_MARKETPLACE_NAME,
                owner_name=_MARKETPLACE_OWNER)

        message = f"agora: publish {bundle.name} v{version}"
        if mode == "3p":
            # 3P(MDM)는 sync 개념이 없고 커밋 SHA만 필요해서 직접 push해요.
            pushed = self.publisher.push_files(
                conn, files, message=message, token=token)
            return PublishResult(
                commit_sha=pushed.commit_sha, version=version, surfaces=surfaces,
                skipped=skipped, files_written=pushed.files_written)

        # managed 기본 경로 — 자동 sync가 "version bump 포함 PR 머지"만 인식해요.
        slug = slugify(bundle.name)
        pr = self.publisher.open_pull_request(
            conn, files, branch=f"agora/publish-{slug}-{version}",
            title=message,
            body=(f"Agora가 생성한 배포예요.\n\n"
                  f"- plugin: `{slug}`\n"
                  f"- version: `{version}` (patch bump)\n"
                  f"- surfaces: {', '.join(surfaces) or '없음'}\n\n"
                  f"이 PR을 default branch에 머지하면 조직 콘솔이 자동 sync해요."),
            token=token)
        return PublishResult(
            commit_sha=pr.commit_sha, version=version, surfaces=surfaces,
            skipped=skipped, files_written=pr.files_written, pr_url=pr.pr_url)

    def handoff(self, bundle) -> dict:
        """관리자가 콘솔에서 할 일을 돌려줘요. Agora 범위 밖 단계의 안내예요."""
        conn = self.connection_service.get()
        repo_slug = ""
        if conn is not None:
            try:
                from .github import parse_owner_repo
                owner, repo = parse_owner_repo(conn.repo_url)
                repo_slug = f"{owner}/{repo}"
            except ValueError:
                repo_slug = ""
        return {
            "console_url": _CONSOLE_URL,
            "repo_slug": repo_slug,
            "marketplace_name": _MARKETPLACE_NAME,
            "plugin_slug": slugify(bundle.name),
            "steps": [
                "PR을 default branch에 머지해요 (자동 sync는 PR 머지만 인식해요).",
                f"{_CONSOLE_URL} 에서 Add plugin → GitHub 를 골라요.",
                f"repo를 `{repo_slug or 'owner/repo'}` 형식으로 입력해요.",
                "marketplace 메뉴에서 'Sync automatically' 를 켜요.",
                "plugin별 installation preference를 정해요 "
                "(Required / 기본설치 / 설치가능 / 미노출).",
            ],
            "requirements": [
                "repo가 private 또는 internal이어야 해요 (public은 조직 marketplace로 못 써요).",
                "해당 repo에 Claude GitHub App이 설치돼 있어야 해요.",
                "자동 sync를 켜려면 GitHub App의 Webhooks(Read & Write) 권한과 "
                "본인의 repo admin 권한이 필요해요.",
                "조직에 Cowork와 Skills가 모두 활성화돼 있어야 해요.",
            ],
        }

    def unpublish(self, bundle) -> PublishResult:
        """bundle이 소유한 plugin 디렉터리를 연결 repo에서 삭제해요.

        연결이 없거나 배포된 적 없으면 no-op. 서피스와 무관하게 모든 루트를 지워요
        (서피스가 바뀐 이력과 M5 옛 경로까지 청소). 공유 marketplace.json은 이 항목만
        빼고 재작성하거나, 마지막 항목이면 파일째 삭제해요.
        """
        conn = self.connection_service.get()
        if conn is None:
            return PublishResult(commit_sha="", version="", files_written=0)
        token = self.connection_service.token_for(conn)
        slug = slugify(bundle.name)
        prefixes = [
            f"{PLUGIN_ROOT}/{slug}",
            f"{DESKTOP_ROOT}/{slug}",
            f"{_LEGACY_PLUGIN_ROOT}/{slug}",     # 옛 경로 마이그레이션
        ]

        existing = self.publisher.read_file(conn, MARKETPLACE_PATH, token=token)
        rewritten = prune_marketplace(existing, slug)
        if rewritten is MARKETPLACE_EMPTY:
            prefixes.append(MARKETPLACE_PATH)
            prefixes.append(LEGACY_MARKETPLACE_PATH)
        elif rewritten is not None:
            self.publisher.push_files(
                conn, {MARKETPLACE_PATH: rewritten},
                message=f"agora: unpublish {bundle.name} (marketplace)", token=token)

        result = self.publisher.delete_paths(
            conn, prefixes,
            message=f"agora: unpublish {bundle.name}", token=token)
        return PublishResult(
            commit_sha=result.commit_sha, version="",
            files_written=result.files_written)
