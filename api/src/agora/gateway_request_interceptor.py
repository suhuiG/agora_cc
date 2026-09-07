"""AgentCore Gateway REQUEST interceptor Lambda entry point."""
from __future__ import annotations

import logging
import os
from collections.abc import Callable
from typing import Any

from .domains.identity.gateway_interceptor import (
    GatewayDenialReason,
    GatewayRequestAuthorizer,
    GatewayRequestDenied,
    HUMAN_CLIENT_IDS_ENV,
    denial_message,
)
from .domains.identity.store import IdentitySchemaTooNew, IdentityStore

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

_identity_store: IdentityStore | None = None
_NOTIFICATION_NOOP = {
    "jsonrpc": "2.0",
    "method": "notifications/initialized",
}


class InterceptorUnavailable(RuntimeError):
    pass


def _method(event: object) -> str:
    if not isinstance(event, dict):
        return "unknown"
    mcp = event.get("mcp")
    if not isinstance(mcp, dict):
        return "unknown"
    gateway_request = mcp.get("gatewayRequest")
    if not isinstance(gateway_request, dict):
        return "unknown"
    body = gateway_request.get("body")
    method = body.get("method") if isinstance(body, dict) else None
    return method if isinstance(method, str) and method else "unknown"


def _request(event: object) -> tuple[object, object]:
    if not isinstance(event, dict) or event.get("interceptorInputVersion") != "1.0":
        return None, None
    mcp = event.get("mcp")
    gateway_request = mcp.get("gatewayRequest") if isinstance(mcp, dict) else None
    if not isinstance(gateway_request, dict):
        return None, None
    return gateway_request.get("body"), gateway_request.get("headers")


def _pass(body: dict) -> dict:
    return {
        "interceptorOutputVersion": "1.0",
        "mcp": {
            "transformedGatewayRequest": {
                "body": body,
            }
        },
    }


def _deny(body: object, reason: GatewayDenialReason) -> dict:
    """거부를 **HTTP 200 + JSON-RPC error** 로 돌려줘요 (IH-128).

    403 이면 안 돼요. MCP Python SDK 의 streamable-HTTP 클라이언트는 `202`·`404` 만 특별
    처리하고 그 밖의 non-2xx 는 `response.raise_for_status()` 로 예외를 던져요
    (`mcp/client/streamable_http.py:358`). 그 지점이 `read_stream_writer` 에 **쓰기 전**이라
    `session.call_tool()` 이 기다리는 응답이 영원히 오지 않아요. 게다가 Strands 는 MCP 세션을
    agent 수명 동안 열어두고 예외가 anyio task group 안에서 나서 traceback 도 안 찍혀요 —
    거부 한 번이 그 컨테이너를 죽을 때까지 무력화했어요(2026-08-30 실측).

    JSON-RPC 는 「전송 성공 + 페이로드 오류」를 200 + `error` 로 표현해요. 그게 규약에 맞는
    모양이고, 그러면 SDK 가 `error` 를 대기 중인 요청에 전달해서 모델이 사용자에게 「이 도구는
    권한이 없어요」를 말할 수 있어요.

    **보안은 안 약해져요** — `transformedGatewayResponse` 를 돌려주는 순간 Gateway 는 대상 MCP
    를 호출하지 않아요. 403↔200 은 「클라이언트가 거부를 어떻게 알게 되나」만 바꿔요.

    기계 판별은 `code` 와 `data.reason` 으로 해요. `reason` 값의 출처는
    `GatewayDenialReason` **닫힌 enum** 이에요 — 이미 로그·화면에서 쓰는 어휘라서 그대로
    내보내도 되고, 원장 id·email 처럼 주체를 특정하는 값은 여기에 실리지 않아요. enum 에 값을
    더할 때 그 규약을 함께 지켜주세요.

    `message` 는 **사유별 한국어 문구**예요 (ADR-0104). 고정 영문이던 동안 모델이 사유를
    추측해서 사용자에게 틀린 설명을 했어요(2026-09-04 실측: 승인 대기를 「현재 계정으로는
    승인되지 않았어요」로 설명). 문구는 `gateway_interceptor.DENIAL_MESSAGES` 한 곳에 있고,
    거기 없는 사유는 예전 고정 문구를 그대로 써요.
    """
    request_id = body.get("id") if isinstance(body, dict) else None
    return {
        "interceptorOutputVersion": "1.0",
        "mcp": {
            "transformedGatewayResponse": {
                "statusCode": 200,
                "body": {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {
                        "code": -32001,
                        "message": denial_message(reason),
                        "data": {"reason": reason.value},
                    },
                },
            }
        },
    }


def process_event(
    event: object,
    *,
    store: IdentityStore,
    now: Callable[[], int] | None = None,
) -> dict:
    body, headers = _request(event)
    method = _method(event)
    try:
        transformed = GatewayRequestAuthorizer(
            store,
            now=now,
        ).authorize_and_transform(body=body, headers=headers)
    except GatewayRequestDenied as exc:
        if exc.reason is GatewayDenialReason.HUMAN_ROUTE_UNCONFIGURED:
            logger.warning(
                "gateway_interceptor_denied method=%s reason=%s "
                "missing_environment=%s",
                method,
                exc.reason.value,
                HUMAN_CLIENT_IDS_ENV,
            )
        else:
            logger.warning(
                "gateway_interceptor_denied method=%s reason=%s",
                method,
                exc.reason.value,
            )
        if method.startswith("notifications/"):
            # A JSON-RPC notification has no id, so a synthetic error response is
            # invalid. Replace the denied operation with a harmless initialized
            # notification so the original operation cannot reach the target.
            return _pass(dict(_NOTIFICATION_NOOP))
        return _deny(body, exc.reason)
    except IdentitySchemaTooNew as exc:
        # 이 Lambda 가 원장을 쓴 쪽보다 낡아요 — 배포 어긋남이라 정책 판정이 아니에요.
        # `InterceptorUnavailable` 로 남겨서 Errors 알람이 울려야 해요. 평범한 거부로
        # 바꾸면 낡은 배포가 정상 거부 트래픽에 섞여 안 보여요.
        #
        # 모델·속성 **이름만** 실어요(스키마예요). 값·handle·헤더는 안 실어요.
        logger.error(
            "gateway_interceptor_stale_code method=%s model=%s unknown=%s "
            "remedy=cdk_deploy_AgoraM2OAuthGateway",
            method,
            exc.model,
            ",".join(exc.unknown),
        )
        raise InterceptorUnavailable(
            "authorization dependency unavailable"
        ) from None
    except Exception as exc:
        # Never include the event, headers, handle, token, or dependency message.
        logger.error(
            "gateway_interceptor_dependency_failure method=%s failure_type=%s",
            method,
            type(exc).__name__,
        )
        raise InterceptorUnavailable(
            "authorization dependency unavailable"
        ) from None
    return _pass(transformed)


def _get_identity_store() -> IdentityStore:
    global _identity_store
    if _identity_store is not None:
        return _identity_store

    table_name = os.environ.get("AGORA_IDENTITY_TABLE", "").strip()
    region = (
        os.environ.get("AGORA_IDENTITY_REGION", "").strip()
        or os.environ.get("AWS_REGION", "").strip()
    )
    if not table_name or not region:
        raise RuntimeError("gateway interceptor identity coordinates are missing")

    import boto3
    from botocore.config import Config

    from .domains.identity.dynamo_store import DynamoIdentityStore

    dynamodb = boto3.resource(
        "dynamodb",
        region_name=region,
        config=Config(
            connect_timeout=0.2,
            read_timeout=0.5,
            retries={"mode": "standard", "total_max_attempts": 2},
        ),
    )
    _identity_store = DynamoIdentityStore(
        table_name=table_name,
        region=region,
        table=dynamodb.Table(table_name),
    )
    return _identity_store


def handler(event: dict[str, Any], _context: Any) -> dict:
    return process_event(event, store=_get_identity_store())
