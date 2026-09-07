import "server-only";

import { createHash } from "node:crypto";
import { DynamoDBClient } from "@aws-sdk/client-dynamodb";
import {
  DeleteCommand,
  DynamoDBDocumentClient,
  GetCommand,
  PutCommand,
} from "@aws-sdk/lib-dynamodb";
import { getAuthConfig } from "./config";

export type OAuthStateRecord = {
  codeVerifier: string;
  returnTo: string;
};

export type SessionRecord = {
  accessToken: string;
  accessTokenExpiresAt: number;
  displayName?: string;
  expiresAt: number;
  refreshToken: string;
};

type StoredOAuthState = {
  PK: string;
  SK: "OAUTH_STATE";
  CodeVerifier: string;
  ExpiresAt: number;
  ReturnTo: string;
};

type StoredSession = {
  PK: string;
  SK: "SESSION";
  AccessToken: string;
  AccessTokenExpiresAt: number;
  DisplayName?: string;
  ExpiresAt: number;
  RefreshToken: string;
};

let cachedClient: DynamoDBDocumentClient | undefined;

function client(): DynamoDBDocumentClient {
  if (!cachedClient) {
    const { region } = getAuthConfig();
    cachedClient = DynamoDBDocumentClient.from(new DynamoDBClient({ region }), {
      marshallOptions: { removeUndefinedValues: true },
    });
  }
  return cachedClient;
}

function key(prefix: "STATE" | "SESSION", opaqueValue: string): string {
  const digest = createHash("sha256").update(opaqueValue, "utf8").digest("hex");
  return `${prefix}#${digest}`;
}

export async function putOAuthState(
  opaqueState: string,
  record: OAuthStateRecord,
  expiresAt: number,
): Promise<void> {
  const { sessionTable } = getAuthConfig();
  const item: StoredOAuthState = {
    PK: key("STATE", opaqueState),
    SK: "OAUTH_STATE",
    CodeVerifier: record.codeVerifier,
    ExpiresAt: expiresAt,
    ReturnTo: record.returnTo,
  };
  await client().send(
    new PutCommand({
      TableName: sessionTable,
      Item: item,
      ConditionExpression: "attribute_not_exists(PK)",
    }),
  );
}

export async function consumeOAuthState(
  opaqueState: string,
): Promise<OAuthStateRecord | null> {
  const { sessionTable } = getAuthConfig();
  const result = await client().send(
    new DeleteCommand({
      TableName: sessionTable,
      Key: { PK: key("STATE", opaqueState), SK: "OAUTH_STATE" },
      ReturnValues: "ALL_OLD",
    }),
  );
  const item = result.Attributes as StoredOAuthState | undefined;
  if (!item || item.ExpiresAt <= Math.floor(Date.now() / 1000)) {
    return null;
  }
  return { codeVerifier: item.CodeVerifier, returnTo: item.ReturnTo };
}

export async function putSession(
  opaqueSessionId: string,
  record: SessionRecord,
): Promise<void> {
  const { sessionTable } = getAuthConfig();
  const item: StoredSession = {
    PK: key("SESSION", opaqueSessionId),
    SK: "SESSION",
    AccessToken: record.accessToken,
    AccessTokenExpiresAt: record.accessTokenExpiresAt,
    DisplayName: record.displayName,
    ExpiresAt: record.expiresAt,
    RefreshToken: record.refreshToken,
  };
  await client().send(
    new PutCommand({
      TableName: sessionTable,
      Item: item,
    }),
  );
}

export async function getSession(
  opaqueSessionId: string,
): Promise<SessionRecord | null> {
  const { sessionTable } = getAuthConfig();
  const result = await client().send(
    new GetCommand({
      TableName: sessionTable,
      Key: { PK: key("SESSION", opaqueSessionId), SK: "SESSION" },
      ConsistentRead: true,
    }),
  );
  const item = result.Item as StoredSession | undefined;
  if (!item) {
    return null;
  }
  return {
    accessToken: item.AccessToken,
    accessTokenExpiresAt: item.AccessTokenExpiresAt,
    displayName: item.DisplayName,
    expiresAt: item.ExpiresAt,
    refreshToken: item.RefreshToken,
  };
}

export async function deleteSession(opaqueSessionId: string): Promise<void> {
  const { sessionTable } = getAuthConfig();
  await client().send(
    new DeleteCommand({
      TableName: sessionTable,
      Key: { PK: key("SESSION", opaqueSessionId), SK: "SESSION" },
    }),
  );
}
