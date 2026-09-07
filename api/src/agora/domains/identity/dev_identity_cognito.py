"""Cognito boundaries for ephemeral local-development OAuth clients."""
from __future__ import annotations

import base64
import hashlib
import json
import urllib.error
import urllib.parse
import urllib.request

from .cognito_client_names import HUMAN_POOL_DEV_CLIENT_PREFIX
from .dev_identity_models import (
    DevIdentityAuthenticationError,
    DevIdentityInfrastructureError,
)


def _post(url: str, *, headers: dict, data: bytes) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


class DevCognitoClientProvisioner:
    def __init__(
        self,
        cognito_client,
        *,
        user_pool_id: str,
        invoke_scope: str,
        token_ttl_minutes: int,
    ) -> None:
        self._cognito = cognito_client
        self._pool_id = user_pool_id
        self._scope = invoke_scope
        self._token_ttl_minutes = token_ttl_minutes

    def provision(self, *, principal: str, blueprint_id: str) -> str:
        digest = hashlib.sha256(
            f"{principal}:{blueprint_id}".encode()
        ).hexdigest()[:24]
        response = self._cognito.create_user_pool_client(
            UserPoolId=self._pool_id,
            ClientName=f"{HUMAN_POOL_DEV_CLIENT_PREFIX}{digest}",
            GenerateSecret=True,
            AllowedOAuthFlowsUserPoolClient=True,
            AllowedOAuthFlows=["client_credentials"],
            AllowedOAuthScopes=[self._scope],
            AccessTokenValidity=self._token_ttl_minutes,
            TokenValidityUnits={"AccessToken": "minutes"},
        )
        return str(response["UserPoolClient"]["ClientId"])

    def delete(self, client_id: str) -> None:
        self._cognito.delete_user_pool_client(
            UserPoolId=self._pool_id,
            ClientId=client_id,
        )


class DevCognitoTokenIssuer:
    def __init__(
        self,
        cognito_client,
        *,
        user_pool_id: str,
        token_url: str,
        invoke_scope: str,
        http_post=None,
    ) -> None:
        self._cognito = cognito_client
        self._pool_id = user_pool_id
        self._token_url = token_url
        self._scope = invoke_scope
        self._http_post = http_post or _post

    def issue(self, client_id: str) -> dict:
        try:
            response = self._cognito.describe_user_pool_client(
                UserPoolId=self._pool_id,
                ClientId=client_id,
            )
            secret = str(response["UserPoolClient"]["ClientSecret"])
            basic = base64.b64encode(f"{client_id}:{secret}".encode()).decode()
            status, body = self._http_post(
                self._token_url,
                headers={
                    "Authorization": f"Basic {basic}",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                data=urllib.parse.urlencode(
                    {
                        "grant_type": "client_credentials",
                        "scope": self._scope,
                    }
                ).encode(),
            )
        except Exception as exc:
            raise DevIdentityInfrastructureError(
                "dev token dependency request failed"
            ) from exc
        if status in (400, 401, 403):
            raise DevIdentityAuthenticationError()
        if status != 200:
            raise DevIdentityInfrastructureError(
                "dev token endpoint unavailable"
            )
        try:
            payload = json.loads(body)
            return {
                "access_token": str(payload["access_token"]),
                "expires_in": int(payload["expires_in"]),
                "token_type": str(payload.get("token_type") or "Bearer"),
            }
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise DevIdentityInfrastructureError(
                "dev token endpoint returned an invalid response"
            ) from exc
