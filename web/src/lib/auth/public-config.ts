export type PublicCognitoConfig = {
  cognitoUserPoolId: string;
  cognitoClientId: string;
  region: string;
};

export function toPublicConfig(config: {
  userPoolId: string;
  clientId: string;
  region: string;
}): PublicCognitoConfig {
  return {
    cognitoUserPoolId: config.userPoolId,
    cognitoClientId: config.clientId,
    region: config.region,
  };
}
