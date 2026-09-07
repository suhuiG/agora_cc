"""S3DynamoSourceStore — S3(콘텐츠) + DynamoDB(메타·버전·manifest·감사) 구현.

S3 레이아웃: s3://{bucket}/{type}/{asset_id}/{semver}/<파일트리>
DDB 키:
  버전 아이템   PK=ASSET#{asset_id}  SK=VER#{sort_key}
불변성: finalize는 status=STAGING 조건부 전이로만 PUBLISHED 확정 →
        이미 PUBLISHED면 ConditionalCheckFailed → VersionAlreadyExists.
"""
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone

import boto3
import boto3.dynamodb.conditions
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError

from .bindings import get_binding
from .models import (
    FileSpec,
    IncompleteUpload,
    Manifest,
    ManifestEntry,
    UploadTicket,
    VersionAlreadyExists,
    VersionNotFound,
    VersionRecord,
    VersionStatus,
)
from .semver import is_valid_semver, sort_key

_PRESIGN_TTL = 3600  # presigned URL 유효시간(초)


def _pk(asset_id: str) -> str:
    return f"ASSET#{asset_id}"


def _ver_sk(version: str) -> str:
    return f"VER#{sort_key(version)}"


class S3DynamoSourceStore:
    """SourceStorePort의 S3 + DynamoDB 구현체."""

    def __init__(self, *, bucket: str, table_name: str, region: str) -> None:
        self._bucket = bucket
        # SigV4 + regional endpoint 강제: presigned URL이 리전 엔드포인트로 서명돼야
        # 브라우저 PUT이 307 리다이렉트 없이 바로 성공해요(버킷이 글로벌 엔드포인트와
        # 다른 리전에 있을 때 발생). virtual addressing으로 path-style 이슈도 피해요.
        self._s3 = boto3.client(
            "s3",
            region_name=region,
            endpoint_url=f"https://s3.{region}.amazonaws.com",
            config=BotoConfig(signature_version="s3v4", s3={"addressing_style": "virtual"}),
        )
        self._table = boto3.resource("dynamodb", region_name=region).Table(table_name)

    def presign_upload(self, asset_id, asset_type, version, files, *, principal, meta=None):
        if not is_valid_semver(version):
            raise ValueError(f"invalid semver: {version!r}")
        # 이미 PUBLISHED면 거부
        existing = self._get_version_item(asset_id, version)
        if existing and existing["status"] == VersionStatus.PUBLISHED.value:
            raise VersionAlreadyExists(f"{asset_id}@{version}")
        upload_id = uuid.uuid4().hex
        prefix = f"{asset_type}/{asset_id}/{version}/"
        self._table.put_item(Item={
            "PK": _pk(asset_id),
            "SK": _ver_sk(version),
            "asset_id": asset_id,
            "asset_type": asset_type,
            "version": version,
            "s3_prefix": prefix,
            "status": VersionStatus.STAGING.value,
            "upload_id": upload_id,
            "declared": {f.path: f.size for f in files},
            "published_by": "",
            "published_at": "",
            "catalog_meta": dict(meta) if meta else {},
        })
        urls = {
            f.path: self._s3.generate_presigned_url(
                "put_object",
                Params={"Bucket": self._bucket, "Key": f"{prefix}{f.path}"},
                ExpiresIn=_PRESIGN_TTL,
            )
            for f in files
        }
        return UploadTicket(
            asset_id=asset_id, version=version, upload_id=upload_id, urls=urls
        )

    # 계약 테스트가 "클라이언트의 S3 PUT"을 흉내 낼 때 쓰는 훅
    def _test_put_object(self, asset_id, version, path, content: bytes) -> None:
        item = self._get_version_item(asset_id, version)
        self._s3.put_object(
            Bucket=self._bucket, Key=f"{item['s3_prefix']}{path}", Body=content
        )

    def _get_version_item(self, asset_id, version) -> dict | None:
        resp = self._table.get_item(Key={"PK": _pk(asset_id), "SK": _ver_sk(version)})
        return resp.get("Item")

    def finalize_version(self, asset_id, version, upload_id, *, principal):
        item = self._get_version_item(asset_id, version)
        if item is None or item.get("upload_id") != upload_id:
            raise VersionNotFound(f"{asset_id}@{version} (upload {upload_id})")
        if item["status"] == VersionStatus.PUBLISHED.value:
            raise VersionAlreadyExists(f"{asset_id}@{version}")
        prefix = item["s3_prefix"]
        entries = []
        for path, size in item["declared"].items():
            key = f"{prefix}{path}"
            try:
                obj = self._s3.get_object(Bucket=self._bucket, Key=key)
            except ClientError as e:
                if e.response["Error"]["Code"] in ("NoSuchKey", "404"):
                    raise IncompleteUpload(f"missing object: {path}") from None
                raise
            body = obj["Body"].read()
            entries.append(ManifestEntry(
                path=path, size=len(body), sha256=hashlib.sha256(body).hexdigest()
            ))
        entries.sort(key=lambda e: e.path)
        manifest = Manifest(entries=tuple(entries))
        get_binding(item["asset_type"]).validate(manifest)
        # 불변 확정: STAGING → PUBLISHED 조건부 전이 (이미 PUBLISHED면 실패)
        try:
            resp = self._table.update_item(
                Key={"PK": _pk(asset_id), "SK": _ver_sk(version)},
                UpdateExpression=(
                    "SET #s = :pub, manifest = :m, published_by = :p, published_at = :t"
                ),
                ConditionExpression="#s = :staging",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={
                    ":pub": VersionStatus.PUBLISHED.value,
                    ":staging": VersionStatus.STAGING.value,
                    ":m": [
                        {"path": e.path, "size": e.size, "sha256": e.sha256} for e in entries
                    ],
                    ":p": principal,
                    # 실제 확정 시각(UTC, 'Z' suffix). 감사·버전 이력 표시에 쓰여요.
                    ":t": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                },
                ReturnValues="ALL_NEW",
            )
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                raise VersionAlreadyExists(f"{asset_id}@{version}") from None
            raise
        return self._to_record(resp["Attributes"])

    def publish_files(
        self, asset_id, asset_type, version, files, *, principal, meta=None
    ):
        specs = [
            FileSpec(path=path, size=len(content))
            for path, content in files.items()
        ]
        ticket = self.presign_upload(
            asset_id,
            asset_type,
            version,
            specs,
            principal=principal,
            meta=meta,
        )
        item = self._get_version_item(asset_id, version)
        prefix = item["s3_prefix"]
        for path, content in files.items():
            self._s3.put_object(
                Bucket=self._bucket,
                Key=f"{prefix}{path}",
                Body=content,
            )
        return self.finalize_version(
            asset_id, version, ticket.upload_id, principal=principal
        )

    def get_manifest(self, asset_id, version):
        item = self._get_version_item(asset_id, version)
        if item is None or "manifest" not in item:
            raise VersionNotFound(f"{asset_id}@{version}")
        return self._manifest_from_item(item)

    def get_file_url(self, asset_id, version, path):
        item = self._get_version_item(asset_id, version)
        if item is None:
            raise VersionNotFound(f"{asset_id}@{version}")
        return self._s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": self._bucket, "Key": f"{item['s3_prefix']}{path}"},
            ExpiresIn=_PRESIGN_TTL,
        )

    def read_file(self, asset_id, version, path):
        item = self._get_version_item(asset_id, version)
        if item is None or item["status"] == VersionStatus.STAGING.value:
            raise VersionNotFound(f"{asset_id}@{version}")
        try:
            obj = self._s3.get_object(Bucket=self._bucket, Key=f"{item['s3_prefix']}{path}")
        except ClientError as e:
            if e.response["Error"]["Code"] in ("NoSuchKey", "404"):
                raise VersionNotFound(f"{asset_id}@{version}:{path}") from None
            raise
        return obj["Body"].read()

    def list_versions(self, asset_id):
        records = [
            self._to_record(item)
            for item in self._list_version_items(asset_id)
            if item["status"] != VersionStatus.STAGING.value
        ]
        return records

    def _list_version_items(self, asset_id: str) -> list[dict]:
        """상태와 무관하게 자산의 모든 버전 item을 페이지네이션해 읽어요.

        일반 목록은 STAGING을 숨기지만 완전 삭제는 실패한 업로드 잔재까지 지워야 해요.
        """
        condition = (
            boto3.dynamodb.conditions.Key("PK").eq(_pk(asset_id))
            & boto3.dynamodb.conditions.Key("SK").begins_with("VER#")
        )
        items: list[dict] = []
        exclusive_start_key = None
        while True:
            kwargs = {"KeyConditionExpression": condition}
            if exclusive_start_key:
                kwargs["ExclusiveStartKey"] = exclusive_start_key
            resp = self._table.query(**kwargs)
            items.extend(resp.get("Items", []))
            exclusive_start_key = resp.get("LastEvaluatedKey")
            if not exclusive_start_key:
                break
        items.sort(key=lambda item: sort_key(item["version"]))
        return items

    def deprecate_version(self, asset_id, version, *, principal):
        item = self._get_version_item(asset_id, version)
        if item is None or item["status"] == VersionStatus.STAGING.value:
            raise VersionNotFound(f"{asset_id}@{version}")
        self._table.update_item(
            Key={"PK": _pk(asset_id), "SK": _ver_sk(version)},
            UpdateExpression="SET #s = :dep",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":dep": VersionStatus.DEPRECATED.value},
        )

    def rollback_version(self, asset_id, version, *, principal):
        # 버전 아이템 삭제 → semver 재사용 가능. S3 객체는 noncurrent lifecycle가 정리.
        self._table.delete_item(Key={"PK": _pk(asset_id), "SK": _ver_sk(version)})

    def purge_asset(self, asset_id: str) -> dict:
        """자산의 모든 버전 DDB 아이템 + S3 소스 객체를 완전 삭제해요(best-effort).

        S3 삭제 실패(prod Object Lock 등)는 s3_errors에 기록하고 계속 진행해요.
        """
        s3_deleted = 0
        s3_errors: list[str] = []
        version_items = self._list_version_items(asset_id)
        for item in version_items:
            prefix = item["s3_prefix"]
            # 1) S3 소스 객체 삭제 (prefix 아래 전부, 페이지네이션)
            try:
                token = None
                while True:
                    kwargs = {"Bucket": self._bucket, "Prefix": prefix}
                    if token:
                        kwargs["ContinuationToken"] = token
                    resp = self._s3.list_objects_v2(**kwargs)
                    keys = [{"Key": o["Key"]} for o in resp.get("Contents", [])]
                    if keys:
                        self._s3.delete_objects(
                            Bucket=self._bucket, Delete={"Objects": keys})
                        s3_deleted += len(keys)
                    token = resp.get("NextContinuationToken")
                    if not token:
                        break
            except Exception as e:
                s3_errors.append(f"{prefix}: {type(e).__name__}: {e}")
            # 2) DDB 버전 아이템 삭제
            self._table.delete_item(Key={
                "PK": item.get("PK", _pk(asset_id)),
                "SK": item.get("SK", _ver_sk(item["version"])),
            })
        return {"versions_deleted": len(version_items),
                "s3_objects_deleted": s3_deleted, "s3_errors": s3_errors}

    @staticmethod
    def _manifest_from_item(item: dict) -> Manifest:
        return Manifest(entries=tuple(
            ManifestEntry(path=e["path"], size=int(e["size"]), sha256=e["sha256"])
            for e in item.get("manifest", [])
        ))

    def _to_record(self, item: dict) -> VersionRecord:
        return VersionRecord(
            asset_id=item["asset_id"],
            version=item["version"],
            asset_type=item["asset_type"],
            s3_prefix=item["s3_prefix"],
            status=VersionStatus(item["status"]),
            manifest=self._manifest_from_item(item),
            published_by=item.get("published_by", ""),
            published_at=item.get("published_at", ""),
            meta=dict(item.get("catalog_meta", {})),
        )
