"""Agora Source Store 패키지.

SourceStorePort 뒤에 S3Dynamo 구현을 두는 구조. 자산 타입 중립.
"""
from .models import (
    FileSpec,
    IncompleteUpload,
    Manifest,
    ManifestEntry,
    SourceStoreError,
    UploadTicket,
    VersionAlreadyExists,
    VersionNotFound,
    VersionRecord,
    VersionStatus,
    reject_credential_path,
    validate_asset_id,
)
from .audit import AuditContext
from .bindings import AssetBinding, get_binding
from .dynamo_store import S3DynamoSourceStore
from .port import SourceStorePort

__all__ = [
    "FileSpec",
    "Manifest",
    "ManifestEntry",
    "UploadTicket",
    "VersionRecord",
    "VersionStatus",
    "reject_credential_path",
    "validate_asset_id",
    "SourceStoreError",
    "VersionAlreadyExists",
    "IncompleteUpload",
    "VersionNotFound",
    "SourceStorePort",
    "AssetBinding",
    "get_binding",
    "AuditContext",
    "S3DynamoSourceStore",
]
