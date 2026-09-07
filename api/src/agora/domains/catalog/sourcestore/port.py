"""SourceStorePort — agora가 소스 저장 백엔드와 대화하는 단일 인터페이스.

RegistryPort의 형제. Mock/S3Dynamo 어댑터가 이 Port를 구현하고, 레이어 코드는
구현을 몰라요. 자산 타입 중립이라 skill·mcp·agent 모두 같은 포트를 써요.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from .models import FileSpec, Manifest, UploadTicket, VersionRecord


@runtime_checkable
class SourceStorePort(Protocol):
    """소스 저장 포트."""

    def presign_upload(
        self,
        asset_id: str,
        asset_type: str,
        version: str,
        files: list[FileSpec],
        *,
        principal: str,
        meta: dict | None = None,
    ) -> UploadTicket:
        """버전 STAGING 생성 + 파일별 presigned PUT URL 발급.

        이미 PUBLISHED된 semver면 VersionAlreadyExists.

        meta는 Store가 해석하지 않는 opaque 카탈로그 메타(name/description/tags 등)
        dict이에요. STAGING 아이템에 저장됐다가 finalize 시 VersionRecord.meta로
        그대로 돌아와요(durable 핸드오프, §11.7).
        """
        ...

    def finalize_version(
        self, asset_id: str, version: str, upload_id: str, *, principal: str
    ) -> VersionRecord:
        """선언 파일 전부 도착·해시 검증 → manifest 작성 → PUBLISHED 확정(원자적).

        누락 객체가 있으면 IncompleteUpload.
        """
        ...

    def publish_files(
        self,
        asset_id: str,
        asset_type: str,
        version: str,
        files: dict[str, bytes],
        *,
        principal: str,
        meta: dict | None = None,
    ) -> VersionRecord:
        """Server-side immutable publish used by trusted generated exports."""
        ...

    def get_manifest(self, asset_id: str, version: str) -> Manifest:
        """버전의 파일 목록(manifest). 없으면 VersionNotFound."""
        ...

    def get_file_url(self, asset_id: str, version: str, path: str) -> str:
        """파일 1건의 presigned GET URL."""
        ...

    def read_file(self, asset_id: str, version: str, path: str) -> bytes:
        """버전의 파일 1건 바이트를 읽어요(설치 archive 구성용, §12).

        버전이 없거나 path가 manifest에 없으면 VersionNotFound.
        """
        ...

    def list_versions(self, asset_id: str) -> list[VersionRecord]:
        """자산의 모든 버전 (semver 정렬). PUBLISHED·DEPRECATED 포함, STAGING 제외."""
        ...

    def deprecate_version(self, asset_id: str, version: str, *, principal: str) -> None:
        """버전을 DEPRECATED로 표시 (콘텐츠는 보존)."""
        ...

    def rollback_version(self, asset_id: str, version: str, *, principal: str) -> None:
        """버전을 완전히 제거해 semver를 재사용 가능하게 해요 (보상 트랜잭션, §11.5).

        finalize에서 source는 PUBLISHED 됐는데 카탈로그 등재가 실패하면 orphan이
        남아요. 그때 이 메서드로 버전 아이템을 삭제해 정합성을 회복해요. 없는 버전
        삭제는 no-op(best-effort). S3 객체는 lifecycle가 정리해요.
        """
        ...

    def purge_asset(self, asset_id: str) -> dict:
        """자산의 모든 버전 DDB 아이템 + S3 소스 객체를 완전 삭제해요(하드 삭제).

        rollback_version은 DDB 1건만 지우고 S3는 lifecycle에 맡기지만, 완전 삭제는
        S3 객체까지 물리 삭제해요. best-effort: S3 삭제 실패(예: prod Object Lock)는
        예외를 던지지 않고 반환 dict의 s3_errors에 기록해요.

        반환: {"versions_deleted": int, "s3_objects_deleted": int, "s3_errors": list[str]}
        """
        ...
