"""DynamoConnectionStore — JsonConnectionStore와 동일 시그니처의 DynamoDB 백엔드.

조직·환경당 단일 레코드라 고정 키(PK=CONNECTION / SK=SINGLETON) 한 아이템만 써요.
로컬 JSON은 Fargate 재시작 시 휘발하므로(HP-03) 공유 DynamoDB로 영속화해요.
PAT 원문은 여기 담지 않아요 — RepoConnection.credential_ref만 저장하고 원문은 CredentialStore.
"""
from __future__ import annotations

from .models import RepoConnection

_PK = "CONNECTION"
_SK = "SINGLETON"


class DynamoConnectionStore:
    def __init__(self, *, table_name, region, client=None) -> None:
        self._table_name = table_name
        self._region = region
        self._table = client

    def _tbl(self):
        if self._table is None:
            import boto3
            self._table = boto3.resource(
                "dynamodb", region_name=self._region).Table(self._table_name)
        return self._table

    def get(self) -> RepoConnection | None:
        item = self._tbl().get_item(Key={"PK": _PK, "SK": _SK}).get("Item")
        return RepoConnection.from_dict(item["data"]) if item else None

    def set(self, conn: RepoConnection) -> None:
        self._tbl().put_item(
            Item={"PK": _PK, "SK": _SK, "data": conn.to_dict()})

    def clear(self) -> None:
        self._tbl().delete_item(Key={"PK": _PK, "SK": _SK})
