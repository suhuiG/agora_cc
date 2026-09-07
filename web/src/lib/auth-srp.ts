"use client";

import {
  AuthenticationDetails,
  CognitoUser,
  CognitoUserPool,
} from "amazon-cognito-identity-js";
import type { CognitoUserSession } from "amazon-cognito-identity-js";

export type SrpTokens = {
  id_token: string;
  access_token: string;
  refresh_token: string;
  expires_in: number;
};

export type SignInResult =
  | { status: "ok"; tokens: SrpTokens }
  | {
      status: "new_password_required";
      complete: (newPassword: string) => Promise<SrpTokens>;
    };

let poolPromise: Promise<CognitoUserPool> | null = null;

async function getPool(): Promise<CognitoUserPool> {
  if (!poolPromise) {
    poolPromise = (async () => {
      const response = await fetch("/api/config", { cache: "no-store" });
      const config: unknown = await response.json();
      if (
        !response.ok ||
        !config ||
        typeof config !== "object" ||
        !("cognitoUserPoolId" in config) ||
        typeof config.cognitoUserPoolId !== "string" ||
        !("cognitoClientId" in config) ||
        typeof config.cognitoClientId !== "string"
      ) {
        throw new Error("Cognito config missing from /api/config");
      }
      return new CognitoUserPool({
        UserPoolId: config.cognitoUserPoolId,
        ClientId: config.cognitoClientId,
      });
    })();
  }
  return poolPromise;
}

export function tokensFromSession(session: CognitoUserSession): SrpTokens {
  const accessToken = session.getAccessToken();
  const expiresIn =
    accessToken.getExpiration() - Math.floor(Date.now() / 1000);

  return {
    id_token: session.getIdToken().getJwtToken(),
    access_token: accessToken.getJwtToken(),
    refresh_token: session.getRefreshToken().getToken(),
    expires_in: expiresIn > 0 ? expiresIn : 900,
  };
}

export async function signIn(
  email: string,
  password: string,
): Promise<SignInResult> {
  const pool = await getPool();
  const user = new CognitoUser({ Username: email, Pool: pool });
  const auth = new AuthenticationDetails({
    Username: email,
    Password: password,
  });

  return new Promise<SignInResult>((resolve, reject) => {
    user.authenticateUser(auth, {
      onSuccess: (session) =>
        resolve({ status: "ok", tokens: tokensFromSession(session) }),
      onFailure: reject,
      newPasswordRequired: () =>
        resolve({
          status: "new_password_required",
          complete: (newPassword) =>
            new Promise<SrpTokens>((complete, fail) => {
              user.completeNewPasswordChallenge(newPassword, {}, {
                onSuccess: (session) =>
                  complete(tokensFromSession(session)),
                onFailure: fail,
              });
            }),
        }),
    });
  });
}

export async function requestPasswordReset(email: string): Promise<void> {
  const pool = await getPool();
  const user = new CognitoUser({ Username: email, Pool: pool });

  return new Promise<void>((resolve, reject) => {
    user.forgotPassword({
      onSuccess: () => resolve(),
      onFailure: reject,
      inputVerificationCode: () => resolve(),
    });
  });
}

export async function confirmPasswordReset(
  email: string,
  code: string,
  newPassword: string,
): Promise<void> {
  const pool = await getPool();
  const user = new CognitoUser({ Username: email, Pool: pool });

  return new Promise<void>((resolve, reject) => {
    user.confirmPassword(code, newPassword, {
      onSuccess: () => resolve(),
      onFailure: reject,
    });
  });
}

export async function clearSrpCache(): Promise<void> {
  const pool = await getPool();
  pool.getCurrentUser()?.signOut();
}
