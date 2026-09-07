import "server-only";

export type AuthConfig = {
  apiUrl: string;
  clientId: string;
  cognitoDomain: string;
  region: string;
  sessionTable: string;
  userPoolId: string;
  webBaseUrl: string;
};

let cachedConfig: AuthConfig | undefined;

function required(name: string): string {
  const value = process.env[name]?.trim();
  if (!value) {
    throw new Error(`${name} is required`);
  }
  return value;
}

function normalizedUrl(name: string): string {
  const value = required(name);
  const url = new URL(value);
  if (!["http:", "https:"].includes(url.protocol)) {
    throw new Error(`${name} must use http or https`);
  }
  return url.toString().replace(/\/$/, "");
}

export function getAuthConfig(): AuthConfig {
  if (cachedConfig) {
    return cachedConfig;
  }
  const mode = process.env.AGORA_WEB_AUTH_MODE?.trim() || "cognito";
  if (mode !== "cognito") {
    throw new Error("AGORA_WEB_AUTH_MODE must be cognito");
  }

  const webBaseUrl = normalizedUrl("AGORA_WEB_BASE_URL");
  const webUrl = new URL(webBaseUrl);
  if (webUrl.pathname !== "/" || webUrl.search || webUrl.hash) {
    throw new Error("AGORA_WEB_BASE_URL must be an origin");
  }

  cachedConfig = {
    apiUrl: normalizedUrl("AGORA_API_URL"),
    clientId: required("AGORA_AUTH_COGNITO_CLIENT_ID"),
    cognitoDomain: normalizedUrl("AGORA_AUTH_COGNITO_DOMAIN"),
    region: process.env.AGORA_AUTH_REGION?.trim() || "ap-northeast-2",
    sessionTable: required("AGORA_AUTH_SESSION_TABLE"),
    userPoolId: required("AGORA_AUTH_COGNITO_USER_POOL_ID"),
    webBaseUrl: webUrl.origin,
  };
  return cachedConfig;
}

export function authCallbackUrl(config = getAuthConfig()): string {
  return `${config.webBaseUrl}/api/auth/callback/cognito`;
}
