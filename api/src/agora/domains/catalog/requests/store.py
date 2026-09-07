"""DynamoRequestLog — PublishRequest를 DynamoDB에 왕복.

배포 job(JOB#)과 같은 테이블(deploy_jobs)에 REQ#{request_id} PK로 얹어요.
목록은 PrincipalRequestIndex를 query하고, GSI가 아직 배포되지 않았거나 백필 중인
과도기에만 scan으로 폴백해 기존 Portal 배포와 인프라 배포의 순서 차이를 흡수해요.
그 밖의 DynamoDB 오류는 숨기지 않습니다.

sync_from_job은 폴러가 배포 job 전진 후 대응 요청 로그의 status를 파생·갱신하는 헬퍼예요.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import time
from datetime import datetime, timezone
from decimal import Decimal

from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.exceptions import ClientError

from ...runtime.deploy.agent_jobs import wait_detail
from ...runtime.deploy.models import DeployJob
from .models import PublishRequest, RequestPage, status_from_phase


_INDEX_NAME = "PrincipalRequestIndex"
_log = logging.getLogger(__name__)
_serializer = TypeSerializer()
_deserializer = TypeDeserializer()


def _encode_cursor(last_key: dict) -> str:
    """DynamoDB key 타입을 잃지 않는 URL-safe opaque cursor."""
    encoded_key = {
        name: _serializer.serialize(value)
        for name, value in last_key.items()
    }
    raw = json.dumps(encoded_key, separators=(",", ":"), sort_keys=True).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(cursor: str) -> dict:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        encoded_key = json.loads(base64.urlsafe_b64decode(padded).decode())
        if not isinstance(encoded_key, dict):
            raise ValueError
        return {
            name: _deserializer.deserialize(value)
            for name, value in encoded_key.items()
        }
    except Exception as exc:
        raise ValueError("invalid request cursor") from exc


def _index_is_missing(exc: Exception) -> bool:
    """실측한 GSI 미배포 오류만 scan 폴백 대상으로 분류해요."""
    if not isinstance(exc, ClientError):
        return False
    error = exc.response.get("Error", {})
    code = str(error.get("Code", ""))
    message = str(error.get("Message", "")).lower()
    if code != "ValidationException" or _INDEX_NAME.lower() not in message:
        return False
    return message.startswith(
        "the table does not have the specified index:"
    )


def _is_validation_error(exc: Exception) -> bool:
    return (
        isinstance(exc, ClientError)
        and exc.response.get("Error", {}).get("Code") == "ValidationException"
    )


def _page_anchor_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_created_at(value: str) -> datetime:
    """초 단위 legacy 값과 microsecond 신규 값을 같은 UTC 축으로 비교해요."""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError):
        return datetime.min.replace(tzinfo=timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _format_created_at(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(
        timespec="microseconds",
    ).replace("+00:00", "Z")


_CURSOR_VERSION = Decimal(3)
_CURSOR_FIELDS = {
    "version",
    "token",
}
_CURSOR_STATE_FIELDS = {
    "version",
    "anchor_created_at",
    "seen_request_ids",
}
_CURSOR_PK_PREFIX = "REQUEST_PAGE_CURSOR#"
_CURSOR_TTL_SECONDS = 15 * 60
# 2,000 IDs at the accepted 128-byte maximum leave headroom below DynamoDB's
# 400 KiB item limit. This bounds server-side snapshot state, not URL length.
_MAX_CURSOR_STATE_REQUEST_IDS = 2000
_CURSOR_TOKEN_PATTERN = re.compile(r"[0-9a-f]{64}")


def _validate_cursor(cursor: dict | None) -> dict | None:
    if cursor is None:
        return None
    if set(cursor) != _CURSOR_FIELDS or cursor.get("version") != _CURSOR_VERSION:
        raise ValueError("invalid request cursor")
    token = cursor.get("token")
    if not isinstance(token, str) or _CURSOR_TOKEN_PATTERN.fullmatch(token) is None:
        raise ValueError("invalid request cursor")
    return cursor


def _validate_cursor_state(state: dict) -> dict:
    if (
        set(state) != _CURSOR_STATE_FIELDS
        or state.get("version") != _CURSOR_VERSION
    ):
        raise ValueError("invalid request cursor")
    anchor = state.get("anchor_created_at")
    seen = state.get("seen_request_ids")
    if not isinstance(anchor, str) or _parse_created_at(anchor) == datetime.min.replace(
        tzinfo=timezone.utc
    ):
        raise ValueError("invalid request cursor")
    if (
        not isinstance(seen, list)
        or len(seen) > _MAX_CURSOR_STATE_REQUEST_IDS
        or len(set(seen)) != len(seen)
        or any(
            not isinstance(request_id, str)
            or not request_id
            or len(request_id) > 128
            for request_id in seen
        )
    ):
        raise ValueError("invalid request cursor")
    return state


def _request_sort_key(request: PublishRequest) -> tuple[datetime, str]:
    return _parse_created_at(request.created_at), request.request_id


def _page_from_snapshot(
    requests: list[PublishRequest],
    *,
    limit: int,
    cursor_state: dict,
) -> tuple[RequestPage, dict | None]:
    """Page an anchored read window without trusting client routing state.

    GSI reads are eventually consistent, so a row absent from page 1 can appear
    on page 2. The cursor therefore carries returned IDs, not an `after` offset:
    later-visible rows at or before the server watermark remain eligible, while
    rows created after the watermark do not move the window.
    """
    requests.sort(key=_request_sort_key, reverse=True)
    state = _validate_cursor_state(cursor_state)

    anchor = _parse_created_at(state["anchor_created_at"])
    seen = set(state["seen_request_ids"])

    snapshot = [
        request for request in requests
        if _request_sort_key(request)[0] <= anchor
    ]
    remaining = [
        request for request in snapshot
        if request.request_id not in seen
    ]

    items = remaining[:limit]
    next_state = None
    if len(remaining) > limit:
        seen.update(request.request_id for request in items)
        next_state = {
            "version": _CURSOR_VERSION,
            "anchor_created_at": _format_created_at(anchor),
            "seen_request_ids": sorted(seen),
        }
    return (
        RequestPage(
            items=items,
            total_count=len(snapshot),
            next_cursor=None,
        ),
        next_state,
    )


class DynamoRequestLog:
    def __init__(self, *, table_name, region, client=None, now=None) -> None:
        self._table_name = table_name
        self._region = region
        self._table = client
        self._now = now or (lambda: int(time.time()))

    def _tbl(self):
        if self._table is None:
            import boto3  # 지연 import
            self._table = boto3.resource(
                "dynamodb", region_name=self._region).Table(self._table_name)
        return self._table

    def put(self, req: PublishRequest) -> None:
        item = req.to_dict()
        item["PK"] = f"REQ#{req.request_id}"
        self._tbl().put_item(Item={k: v for k, v in item.items() if v is not None})

    def get(self, request_id: str) -> PublishRequest | None:
        resp = self._tbl().get_item(Key={"PK": f"REQ#{request_id}"})
        item = resp.get("Item")
        if not item:
            return None
        item.pop("PK", None)
        return PublishRequest.from_dict(item)

    def _scan_reqs(self) -> list[PublishRequest]:
        """과도기·역참조용 전체 scan. LastEvaluatedKey를 끝까지 소비해요."""
        items = []
        kwargs = {}
        while True:
            response = self._tbl().scan(**kwargs)
            items.extend(response.get("Items", []))
            last_key = response.get("LastEvaluatedKey")
            if not last_key:
                break
            kwargs["ExclusiveStartKey"] = last_key
        out = []
        for it in items:
            pk = it.get("PK", "")
            if not pk.startswith("REQ#"):
                continue
            out.append(PublishRequest.from_dict({
                key: value for key, value in it.items() if key != "PK"
            }))
        return out

    def list_by_principal(self, principal: str) -> list[PublishRequest]:
        """내부 동기화 호출용 전체 목록. API는 page_by_principal을 사용해요."""
        items = []
        cursor = None
        while True:
            page = self.page_by_principal(principal, limit=100, cursor=cursor)
            items.extend(page.items)
            cursor = page.next_cursor
            if cursor is None:
                return items

    @staticmethod
    def _cursor_token(principal: str, state: dict) -> str:
        material = f"{principal}\0{_encode_cursor(state)}".encode()
        return hashlib.sha256(material).hexdigest()

    def _load_cursor_state(self, principal: str, cursor: str) -> dict:
        envelope = _validate_cursor(_decode_cursor(cursor))
        response = self._tbl().get_item(
            Key={"PK": f"{_CURSOR_PK_PREFIX}{envelope['token']}"},
            ConsistentRead=True,
        )
        item = response.get("Item")
        if (
            not item
            or item.get("owner_principal") != principal
            or int(item.get("expires_at") or 0) <= self._now()
        ):
            raise ValueError("invalid request cursor")
        state = _validate_cursor_state({
            "version": item.get("cursor_version"),
            "anchor_created_at": item.get("anchor_created_at"),
            "seen_request_ids": item.get("seen_request_ids"),
        })
        if self._cursor_token(principal, state) != envelope["token"]:
            raise ValueError("invalid request cursor")
        # Active page polling extends the snapshot lease instead of expiring a
        # cursor that is still being used.
        self._store_cursor_state(principal, state)
        return state

    def _store_cursor_state(self, principal: str, state: dict) -> str:
        state = _validate_cursor_state(state)
        token = self._cursor_token(principal, state)
        self._tbl().put_item(Item={
            "PK": f"{_CURSOR_PK_PREFIX}{token}",
            # Do not use the GSI key name `principal`: cursor rows must stay
            # outside PrincipalRequestIndex and its request query.
            "owner_principal": principal,
            "cursor_version": _CURSOR_VERSION,
            "anchor_created_at": state["anchor_created_at"],
            "seen_request_ids": state["seen_request_ids"],
            "expires_at": self._now() + _CURSOR_TTL_SECONDS,
        })
        return _encode_cursor({
            "version": _CURSOR_VERSION,
            "token": token,
        })

    def page_by_principal(
        self,
        principal: str,
        *,
        limit: int = 10,
        cursor: str | None = None,
    ) -> RequestPage:
        if limit < 1:
            raise ValueError("limit must be positive")
        if cursor:
            cursor_state = self._load_cursor_state(principal, cursor)
        else:
            # Query가 진행되는 동안 생성된 요청도 이 페이지 창에 들어오면 안 돼요.
            cursor_state = {
                "version": _CURSOR_VERSION,
                "anchor_created_at": _format_created_at(_page_anchor_now()),
                "seen_request_ids": [],
            }
        try:
            page, next_state = self._query_page(
                principal,
                limit=limit,
                cursor_state=cursor_state,
            )
        except ClientError as exc:
            unavailable = _index_is_missing(exc)
            if not unavailable and _is_validation_error(exc):
                unavailable = self._index_is_backfilling()
            if not unavailable:
                raise
            _log.warning(
                "%s is unavailable; falling back to a full request-log scan",
                _INDEX_NAME,
            )
            page, next_state = self._scan_page(
                principal,
                limit=limit,
                cursor_state=cursor_state,
            )
        next_cursor = (
            self._store_cursor_state(principal, next_state)
            if next_state is not None
            else None
        )
        return RequestPage(
            items=page.items,
            total_count=page.total_count,
            next_cursor=next_cursor,
        )

    def _index_is_backfilling(self) -> bool:
        """Read the structured DescribeTable backfill state.

        We intentionally do not match an unverified Query error string. AWS
        documents `GlobalSecondaryIndexes[].Backfilling` as the independent
        source of truth for an index added to an existing table.
        """
        client = getattr(getattr(self._tbl(), "meta", None), "client", None)
        if client is None:
            return False
        try:
            response = client.describe_table(TableName=self._table_name)
        except Exception:
            _log.warning(
                "Unable to observe %s backfill status; refusing scan fallback",
                _INDEX_NAME,
                exc_info=True,
            )
            return False
        indexes = response.get("Table", {}).get("GlobalSecondaryIndexes", [])
        return any(
            index.get("IndexName") == _INDEX_NAME
            and index.get("IndexStatus") == "CREATING"
            and index.get("Backfilling") is True
            for index in indexes
        )

    def _query_page(
        self,
        principal: str,
        *,
        limit: int,
        cursor_state: dict,
    ) -> tuple[RequestPage, dict | None]:
        names = {"#principal": "principal", "#pk": "PK"}
        values = {":principal": principal, ":request_prefix": "REQ#"}
        common = {
            "IndexName": _INDEX_NAME,
            "KeyConditionExpression": (
                "#principal = :principal AND begins_with(#pk, :request_prefix)"
            ),
            "ExpressionAttributeNames": names,
            "ExpressionAttributeValues": values,
        }

        raw_items = []
        query_start = None
        while True:
            query_args = {
                **common,
                "ExpressionAttributeNames": {
                    **names,
                    "#status": "status",
                    "#error": "error",
                },
                "ProjectionExpression": (
                    "#pk, request_id, #principal, kind, #status, title, "
                    "created_at, updated_at, record_id, job_id, #error, "
                    "phase, phase_detail"
                ),
            }
            if query_start:
                query_args["ExclusiveStartKey"] = query_start
            response = self._tbl().query(**query_args)
            raw_items.extend(response.get("Items", []))
            query_start = response.get("LastEvaluatedKey")
            if not query_start:
                break

        requests = [
            PublishRequest.from_dict({
                key: value for key, value in item.items() if key != "PK"
            })
            for item in raw_items
        ]
        return _page_from_snapshot(
            requests,
            limit=limit,
            cursor_state=cursor_state,
        )

    def _scan_page(
        self,
        principal: str,
        *,
        limit: int,
        cursor_state: dict,
    ) -> tuple[RequestPage, dict | None]:
        got = [r for r in self._scan_reqs() if r.principal == principal]
        return _page_from_snapshot(
            got,
            limit=limit,
            cursor_state=cursor_state,
        )

    def find_by_job(self, job_id: str) -> PublishRequest | None:
        for r in self._scan_reqs():
            if r.job_id == job_id:
                return r
        return None

    def delete_by_record(self, record_id: str) -> int:
        """해당 record_id의 요청 로그 항목을 모두 삭제해요. 삭제 개수 반환(하드 삭제 정리용)."""
        n = 0
        for r in self._scan_reqs():
            if r.record_id == record_id:
                self._tbl().delete_item(Key={"PK": f"REQ#{r.request_id}"})
                n += 1
        return n


def sync_from_job(log, job: DeployJob, now) -> PublishRequest | None:
    """배포 job 전진 후 대응 요청 로그의 status/record_id/error를 파생·갱신해요.

    log.find_by_job으로 요청을 찾아 갱신하고 put. 없으면 None(로그가 안 남은 과거 job).
    """
    req = log.find_by_job(job.job_id)
    if req is None:
        return None
    req.status = status_from_phase(job.phase)
    req.record_id = job.record_id
    req.error = job.error
    # IH-77: 화면이 phase 이름만 보면 6분 걸리는 배포를 장애로 오인해요(실측 2026-08-21).
    # 무엇을 기다리는지와 몇 번째 관측인지를 함께 남겨요.
    req.phase = job.phase.value if hasattr(job.phase, "value") else str(job.phase)
    req.phase_detail = wait_detail(job)
    req.updated_at = now()
    log.put(req)
    return req
