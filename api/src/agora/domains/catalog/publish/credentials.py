# api/src/agora/domains/catalog/publish/credentials.py
"""CredentialStore — PAT 등 비밀을 ref 키로 격리 보관.

로컬 개발은 JsonCredentialStore(파일), 프로덕션은 후속으로 SecretsManagerCredentialStore.
RepoConnection에는 ref만 남고 원문은 여기에만 있어요.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol, runtime_checkable


@runtime_checkable
class CredentialStore(Protocol):
    def put(self, ref: str, secret: str) -> None: ...
    def get(self, ref: str) -> str: ...
    def delete(self, ref: str) -> None: ...


class JsonCredentialStore:
    """로컬 JSON 파일 구현. .gitignore된 .agora-*.json 계열에 둬요(원문 평문 주의)."""

    def __init__(self, store_path: str | Path) -> None:
        self._store_path = Path(store_path)
        self._secrets: dict[str, str] = {}
        if self._store_path.exists():
            self._secrets = json.loads(self._store_path.read_text())

    def put(self, ref: str, secret: str) -> None:
        self._secrets[ref] = secret
        self._persist()

    def get(self, ref: str) -> str:
        return self._secrets[ref]

    def delete(self, ref: str) -> None:
        self._secrets.pop(ref, None)
        self._persist()

    def _persist(self) -> None:
        self._store_path.write_text(json.dumps(self._secrets, ensure_ascii=False, indent=2))


class SecretsManagerCredentialStore:
    """AWS Secrets Manager 구현 — PAT을 secret으로 격리 보관(프로덕션).

    secret 이름은 {prefix}/{ref}. client는 테스트 주입용(기본 boto3 지연 생성).
    """

    def __init__(self, *, region: str, prefix: str = "agora/publish", client=None) -> None:
        self._region = region
        self._prefix = prefix
        self._client = client

    def _sm(self):
        if self._client is None:
            import boto3
            self._client = boto3.client("secretsmanager", region_name=self._region)
        return self._client

    def _name(self, ref: str) -> str:
        return f"{self._prefix}/{ref}"

    def put(self, ref: str, secret: str) -> None:
        from botocore.exceptions import ClientError
        name = self._name(ref)
        try:
            self._sm().create_secret(Name=name, SecretString=secret)
        except ClientError as e:
            if e.response["Error"]["Code"] in (
                    "ResourceExistsException", "InvalidRequestException"):
                self._sm().put_secret_value(SecretId=name, SecretString=secret)
            else:
                raise

    def get(self, ref: str) -> str:
        from botocore.exceptions import ClientError
        try:
            resp = self._sm().get_secret_value(SecretId=self._name(ref))
        except ClientError as e:
            if e.response["Error"]["Code"] == "ResourceNotFoundException":
                raise KeyError(ref) from None
            raise
        return resp["SecretString"]

    def delete(self, ref: str) -> None:
        from botocore.exceptions import ClientError
        try:
            self._sm().delete_secret(
                SecretId=self._name(ref), ForceDeleteWithoutRecovery=True)
        except ClientError as e:
            if e.response["Error"]["Code"] != "ResourceNotFoundException":
                raise
