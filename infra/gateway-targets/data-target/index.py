"""CRUD Lambda target behind the AgentCore Gateway — USER/ITEM tools (8 tools)."""
from __future__ import annotations

import os
import re

import boto3
from boto3.dynamodb.conditions import Key

_USER_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_ORDER_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_dynamodb = boto3.resource("dynamodb")
_users = _dynamodb.Table(os.environ["USER_TABLE_NAME"])
_items = _dynamodb.Table(os.environ["ITEM_TABLE_NAME"])

_DEMO_NOTE = "데모: 권한 허용을 확인했어요. 실제 변경은 수행하지 않아요."


def _tool_name(context) -> str:
    client_context = getattr(context, "client_context", None)
    custom = getattr(client_context, "custom", None) or {}
    full_name = str(custom.get("bedrockAgentCoreToolName") or "")
    return full_name.rpartition("___")[2]


def _user_id(event: dict) -> str:
    value = str(event.get("user_id") or "").strip()
    if not _USER_ID.fullmatch(value):
        raise ValueError("user_id must contain 1-64 letters, digits, '_' or '-'")
    return value


def _order_id(event: dict) -> str:
    value = str(event.get("order_id") or "").strip()
    if not _ORDER_ID.fullmatch(value):
        raise ValueError("order_id must contain 1-64 letters, digits, '_' or '-'")
    return value


def _item_to_user(item: dict) -> dict:
    return {
        "user_id": item.get("user_id"),
        "name": item.get("name"),
        "department": item.get("department"),
        "location": item.get("location"),
    }


def _item_to_order(item: dict) -> dict:
    return {
        "user_id": item.get("user_id"),
        "order_id": item.get("order_id"),
        "item_name": item.get("item_name"),
        "status": item.get("status"),
    }


# ---------------------------------------------------------------------------
# USER tools
# ---------------------------------------------------------------------------

def _list_users(event: dict) -> dict:
    limit = int(event.get("limit") or 50)
    resp = _users.scan()
    users = [_item_to_user(i) for i in resp.get("Items", [])]
    return {"users": users[:limit]}


def _get_user(event: dict) -> dict:
    uid = _user_id(event)
    resp = _users.get_item(Key={"user_id": uid}, ConsistentRead=True)
    item = resp.get("Item")
    return {"user": _item_to_user(item) if item else None}


def _put_user(event: dict) -> dict:
    uid = _user_id(event)
    return {
        "acknowledged": True,
        "written": False,
        "note": _DEMO_NOTE,
        "would_write": {
            "user_id": uid,
            "name": event.get("name"),
            "department": event.get("department"),
            "location": event.get("location"),
        },
    }


def _delete_user(event: dict) -> dict:
    uid = _user_id(event)
    return {
        "acknowledged": True,
        "deleted": False,
        "note": _DEMO_NOTE,
        "would_delete": {"user_id": uid},
    }


# ---------------------------------------------------------------------------
# ITEM tools
# ---------------------------------------------------------------------------

def _list_items(event: dict) -> dict:
    limit = int(event.get("limit") or 50)
    raw_uid = str(event.get("user_id") or "").strip()
    if raw_uid and _USER_ID.fullmatch(raw_uid):
        resp = _items.query(
            KeyConditionExpression=Key("user_id").eq(raw_uid),
            ConsistentRead=True,
        )
    else:
        resp = _items.scan()
    orders = sorted(
        [_item_to_order(i) for i in resp.get("Items", [])],
        key=lambda o: str(o.get("order_id") or ""),
    )
    return {"orders": orders[:limit]}


def _get_item(event: dict) -> dict:
    uid = _user_id(event)
    oid = _order_id(event)
    resp = _items.get_item(
        Key={"user_id": uid, "order_id": oid},
        ConsistentRead=True,
    )
    item = resp.get("Item")
    return {"order": _item_to_order(item) if item else None}


def _put_item(event: dict) -> dict:
    uid = _user_id(event)
    oid = _order_id(event)
    return {
        "acknowledged": True,
        "written": False,
        "note": _DEMO_NOTE,
        "would_write": {
            "user_id": uid,
            "order_id": oid,
            "item_name": event.get("item_name"),
            "status": event.get("status"),
        },
    }


def _delete_item(event: dict) -> dict:
    uid = _user_id(event)
    oid = _order_id(event)
    return {
        "acknowledged": True,
        "deleted": False,
        "note": _DEMO_NOTE,
        "would_delete": {"user_id": uid, "order_id": oid},
    }


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

_HANDLERS = {
    "list_users": _list_users,
    "get_user": _get_user,
    "put_user": _put_user,
    "delete_user": _delete_user,
    "list_items": _list_items,
    "get_item": _get_item,
    "put_item": _put_item,
    "delete_item": _delete_item,
}


def lambda_handler(event, context):
    if not isinstance(event, dict):
        event = {}
    tool_name = _tool_name(context)
    handler = _HANDLERS.get(tool_name)
    if handler is None:
        raise ValueError(f"unsupported tool: {tool_name}")
    return handler(event)
