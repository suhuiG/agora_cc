import * as path from "path";
import * as cdk from "aws-cdk-lib";
import * as cognito from "aws-cdk-lib/aws-cognito";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import * as iam from "aws-cdk-lib/aws-iam";
import * as lambda from "aws-cdk-lib/aws-lambda";
import { Construct } from "constructs";
import { Stage } from "./catalog-storage-stack";
import {
  cognitoDomainPrefix,
  cognitoUserPoolName,
  m2OAuthInvokeScope,
  m2OAuthResourceServerIdentifier,
} from "./cognito-domain";

export interface IdentityStackProps extends cdk.StackProps {
  readonly stage: Stage;
  readonly webBaseUrl?: string;
  readonly apiExecutionRole: iam.IRole;
}

export function identityDataTableName(stage: Stage): string {
  return `agora-identity-${stage}`;
}

/**
 * Human identity only. Runtime/Gateway workload authentication remains in
 * RuntimeDeployStack and must not be used as an end-user principal.
 */
export class IdentityStack extends cdk.Stack {
  public readonly userPool: cognito.UserPool;
  public readonly userPoolClient: cognito.UserPoolClient;
  public readonly authSessionTable: dynamodb.Table;
  public readonly identityDataTable: dynamodb.Table;
  public readonly permissionClaimsFunction: lambda.Function;

  constructor(scope: Construct, id: string, props: IdentityStackProps) {
    super(scope, id, props);

    const isProd = props.stage === "prod";
    const removalPolicy = isProd
      ? cdk.RemovalPolicy.RETAIN
      : cdk.RemovalPolicy.DESTROY;
    if (isProd && !props.webBaseUrl) {
      throw new Error(
        "prod requires webBaseUrl; pass the PortalStack CloudFront URL or an explicit public origin",
      );
    }
    const webBaseUrl = normalizeWebBaseUrl(
      props.webBaseUrl ?? "http://localhost:3000",
      isProd,
    );

    this.authSessionTable = new dynamodb.Table(this, "HumanAuthSessionTable", {
      tableName: `agora-human-auth-session-${props.stage}`,
      partitionKey: { name: "PK", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "SK", type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      timeToLiveAttribute: "ExpiresAt",
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
      removalPolicy,
    });

    this.identityDataTable = new dynamodb.Table(this, "IdentityDataTable", {
      tableName: identityDataTableName(props.stage),
      partitionKey: { name: "PK", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "SK", type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      timeToLiveAttribute: "ExpiresAt",
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
      removalPolicy,
    });
    this.identityDataTable.addGlobalSecondaryIndex({
      indexName: "AuditTimelineIndex",
      partitionKey: { name: "GSI1PK", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "GSI1SK", type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });
    // Keep this policy in Identity so Catalog does not depend on Identity.
    // Portal can then provide its CloudFront origin without creating a stack cycle.
    const apiIdentityPolicy = new iam.Policy(this, "ApiIdentityAccessPolicy", {
      roles: [props.apiExecutionRole],
      statements: [
        new iam.PolicyStatement({
          actions: [
            "dynamodb:BatchGetItem",
            "dynamodb:BatchWriteItem",
            "dynamodb:ConditionCheckItem",
            "dynamodb:DeleteItem",
            "dynamodb:DescribeTable",
            "dynamodb:GetItem",
            "dynamodb:GetRecords",
            "dynamodb:GetShardIterator",
            "dynamodb:PutItem",
            "dynamodb:Query",
            "dynamodb:Scan",
            "dynamodb:UpdateItem",
          ],
          resources: [
            this.identityDataTable.tableArn,
            `${this.identityDataTable.tableArn}/index/*`,
          ],
        }),
      ],
    });

    this.permissionClaimsFunction = new lambda.Function(
      this,
      "PermissionClaims",
      {
        functionName: `agora-perms-claim-${props.stage}`,
        runtime: lambda.Runtime.PYTHON_3_12,
        architecture: lambda.Architecture.ARM_64,
        handler: "agora.permission_claims.handler",
        code: lambda.Code.fromAsset(path.join(__dirname, "../../api/src"), {
          exclude: ["**/__pycache__", "**/*.pyc"],
        }),
        memorySize: 256,
        timeout: cdk.Duration.seconds(5),
        environment: {
          AGORA_ROLE: "lambda",
          AGORA_IDENTITY_TABLE: this.identityDataTable.tableName,
          AGORA_IDENTITY_REGION: this.region,
          // IA-75 ②: pre-token Lambda 가 사람 토큰에 이 scope 를 `scopesToAdd` 로 실어
          // Gateway `allowedScopes=[…/invoke]` 입장 조건을 만족시켜요. 값은 [S] 공유 좌표라
          // stage 로 파생하는 helper 에서 가져와요 — M2 OAuth Gateway 의 allowedScopes 와
          // 같은 함수(`m2OAuthInvokeScope`)를 써서 두 값이 어긋나지 않아요.
          AGORA_M2_OAUTH_SCOPE: m2OAuthInvokeScope(props.stage),
        },
      },
    );
    this.identityDataTable.grant(
      this.permissionClaimsFunction,
      "dynamodb:Query",
    );

    this.userPool = new cognito.UserPool(this, "HumanUserPool", {
      userPoolName: cognitoUserPoolName(props.stage, "human"),
      selfSignUpEnabled: false,
      signInAliases: { email: true },
      autoVerify: { email: true },
      signInCaseSensitive: false,
      accountRecovery: cognito.AccountRecovery.EMAIL_ONLY,
      mfa: isProd ? cognito.Mfa.REQUIRED : cognito.Mfa.OPTIONAL,
      mfaSecondFactor: { sms: false, otp: true },
      passwordPolicy: {
        minLength: 12,
        requireLowercase: true,
        requireUppercase: true,
        requireDigits: true,
        requireSymbols: true,
        tempPasswordValidity: cdk.Duration.days(3),
      },
      standardAttributes: {
        email: { required: true, mutable: true },
      },
      // 소유 팀. 자산 등록 시 owner_team 기본값과 감사·에스컬레이션 기준으로 써요.
      //
      // Cognito custom attribute는 **비가역**이에요: 한 번 만들면 삭제·이름 변경·mutable
      // 변경이 불가하고, User Pool당 최대 50개예요. 반면 추가 자체는 CloudFormation
      // `Schema` 업데이트가 "No interruption"이라 기존 사용자·풀은 그대로 유지돼요
      // (AWS::Cognito::UserPool 리소스 문서 확인, 2026-08-09).
      //
      // 값의 원천(인사 IdP attribute mapping)은 아직 없어요. 그래서 값이 비는 게 정상이고,
      // 백엔드는 team=""을 "정보 없음"으로 다뤄 등록 폼의 자유입력으로 폴백해요.
      customAttributes: {
        team: new cognito.StringAttribute({ minLen: 0, maxLen: 128, mutable: true }),
      },
      removalPolicy,
    });
    apiIdentityPolicy.addStatements(
      new iam.PolicyStatement({
        actions: [
          "cognito-idp:ListUsers",
          "cognito-idp:AdminGetUser",
          "cognito-idp:AdminCreateUser",
          "cognito-idp:AdminUpdateUserAttributes",
          "cognito-idp:AdminAddUserToGroup",
          "cognito-idp:AdminRemoveUserFromGroup",
          "cognito-idp:AdminListGroupsForUser",
          "cognito-idp:AdminDisableUser",
          "cognito-idp:AdminEnableUser",
          "cognito-idp:AdminResetUserPassword",
        ],
        resources: [this.userPool.userPoolArn],
      }),
    );
    // IA-77: permission_claims.handler는 event.version === "2"만 받아요. 이 값을
    // V3_0으로 올리면 사람 pool의 로그인·refresh를 포함한 모든 토큰 발급이 실패해요.
    // V3 이벤트 지원을 핸들러와 회귀에 먼저 추가하기 전에는 V2_0을 유지해야 해요.
    this.userPool.addTrigger(
      cognito.UserPoolOperation.PRE_TOKEN_GENERATION_CONFIG,
      this.permissionClaimsFunction,
      cognito.LambdaVersion.V2_0,
    );

    // IA-75 ①: 사람 pool 에 M2 OAuth Gateway 와 **같은** resource server 를 등록해요.
    // 그래야 pre-token Lambda 가 얹는 `…/invoke` scope 가 실재하는 scope 가 되고, 사람
    // 토큰이 Gateway 입장 조건(`allowedScopes`)을 만족할 수 있어요. identifier·scope 는
    // M2 OAuth Gateway 와 같은 helper(`cognito-domain.ts`)에서 와서 두 pool 이 어긋나지
    // 않아요. resource server name 은 URL 이 아니에요 — Cognito 가 name 을 [\w\s+=,.@-]+
    // 로 제한하거든요(M2 스택과 동일 규약).
    this.userPool.addResourceServer("HumanM2OAuthResourceServer", {
      userPoolResourceServerName: `agora-m2-oauth-${props.stage}`,
      identifier: m2OAuthResourceServerIdentifier(props.stage),
      scopes: [
        {
          scopeName: "invoke",
          scopeDescription: "Invoke MCP tool via M2 OAuth Gateway",
        },
      ],
    });

    new cognito.CfnUserPoolGroup(this, "AdminGroup", {
      userPoolId: this.userPool.userPoolId,
      groupName: "admin",
      description: "Agora platform administrators",
      precedence: 0,
    });
    new cognito.CfnUserPoolGroup(this, "UserGroup", {
      userPoolId: this.userPool.userPoolId,
      groupName: "user",
      description: "Agora asset publishers and consumers",
      precedence: 10,
    });

    const callbackUrl = `${webBaseUrl}/api/auth/callback/cognito`;
    this.userPoolClient = new cognito.UserPoolClient(this, "HumanWebClient", {
      userPool: this.userPool,
      userPoolClientName: `agora-human-web-${props.stage}`,
      generateSecret: false,
      preventUserExistenceErrors: true,
      enableTokenRevocation: true,
      supportedIdentityProviders: [
        cognito.UserPoolClientIdentityProvider.COGNITO,
      ],
      accessTokenValidity: cdk.Duration.minutes(15),
      idTokenValidity: cdk.Duration.minutes(15),
      refreshTokenValidity: cdk.Duration.hours(8),
      oAuth: {
        flows: { authorizationCodeGrant: true },
        scopes: [
          cognito.OAuthScope.OPENID,
          cognito.OAuthScope.EMAIL,
          cognito.OAuthScope.PROFILE,
        ],
        callbackUrls: [callbackUrl],
        logoutUrls: [webBaseUrl],
        defaultRedirectUri: callbackUrl,
      },
    });
    const cfnClient = this.userPoolClient.node.defaultChild as cognito.CfnUserPoolClient;
    // 기존 public client에 SRP만 병행해 자체 폼과 Managed Login이 같은 client_id를 쓴다.
    // USER_PASSWORD_AUTH는 평문 비밀번호 인증 경로이므로 열지 않는다.
    cfnClient.explicitAuthFlows = [
      "ALLOW_REFRESH_TOKEN_AUTH",
      "ALLOW_USER_SRP_AUTH",
    ];

    const domainPrefix = cognitoDomainPrefix(this, props.stage, "human");
    const domain = this.userPool.addDomain("HumanManagedLoginDomain", {
      cognitoDomain: {
        domainPrefix,
      },
      managedLoginVersion: cognito.ManagedLoginVersion.NEWER_MANAGED_LOGIN,
    });
    const branding = new cognito.CfnManagedLoginBranding(
      this,
      "HumanManagedLoginBranding",
      {
        userPoolId: this.userPool.userPoolId,
        clientId: this.userPoolClient.userPoolClientId,
        useCognitoProvidedValues: false,
        settings: {
          categories: {
            form: {
              displayGraphics: false,
              location: {
                horizontal: "CENTER",
                vertical: "CENTER",
              },
            },
            global: {
              colorSchemeMode: "LIGHT",
              pageFooter: { enabled: false },
              pageHeader: { enabled: false },
              spacingDensity: "REGULAR",
            },
          },
          componentClasses: {
            buttons: {
              borderRadius: 8,
            },
            focusState: {
              lightMode: {
                borderColor: "2563ebff",
              },
            },
            input: {
              borderRadius: 8,
              lightMode: {
                defaults: {
                  backgroundColor: "ffffffff",
                  borderColor: "cbd5e1ff",
                },
                placeholderColor: "64748bff",
              },
            },
            inputDescription: {
              lightMode: {
                textColor: "64748bff",
              },
            },
            inputLabel: {
              lightMode: {
                textColor: "1e293bff",
              },
            },
            link: {
              lightMode: {
                defaults: {
                  textColor: "2563ebff",
                },
                hover: {
                  textColor: "1d4ed8ff",
                },
              },
            },
          },
          components: {
            form: {
              backgroundImage: {
                enabled: false,
              },
              borderRadius: 8,
              lightMode: {
                backgroundColor: "ffffffff",
                borderColor: "e2e8f0ff",
              },
              logo: {
                enabled: false,
                formInclusion: "IN",
                location: "CENTER",
                position: "TOP",
              },
            },
            pageBackground: {
              image: {
                enabled: false,
              },
              lightMode: {
                color: "f8fafcff",
              },
            },
            pageText: {
              lightMode: {
                bodyColor: "1e293bff",
                descriptionColor: "64748bff",
                headingColor: "0f172aff",
              },
            },
            primaryButton: {
              lightMode: {
                active: {
                  backgroundColor: "020617ff",
                  textColor: "f8fafcff",
                },
                defaults: {
                  backgroundColor: "0f172aff",
                  textColor: "f8fafcff",
                },
                disabled: {
                  backgroundColor: "f1f5f9ff",
                  borderColor: "e2e8f0ff",
                },
                hover: {
                  backgroundColor: "1e293bff",
                  textColor: "f8fafcff",
                },
              },
            },
          },
        },
      },
    );
    branding.addDependency(cfnClient);
    branding.addDependency(
      domain.node.defaultChild as cognito.CfnUserPoolDomain,
    );
    const issuer =
      `https://cognito-idp.${this.region}.${cdk.Aws.URL_SUFFIX}` +
      `/${this.userPool.userPoolId}`;

    new cdk.CfnOutput(this, "HumanUserPoolId", {
      value: this.userPool.userPoolId,
    });
    new cdk.CfnOutput(this, "HumanUserPoolArn", {
      value: this.userPool.userPoolArn,
    });
    new cdk.CfnOutput(this, "HumanCognitoIssuer", { value: issuer });
    new cdk.CfnOutput(this, "HumanCognitoClientId", {
      value: this.userPoolClient.userPoolClientId,
    });
    new cdk.CfnOutput(this, "HumanCognitoDomain", {
      value: `https://${domainPrefix}.auth.${this.region}.amazoncognito.com`,
    });
    new cdk.CfnOutput(this, "HumanCognitoCallbackUrl", {
      value: callbackUrl,
    });
    new cdk.CfnOutput(this, "HumanAuthSessionTableName", {
      value: this.authSessionTable.tableName,
    });
    new cdk.CfnOutput(this, "IdentityDataTableName", {
      value: this.identityDataTable.tableName,
    });
  }
}

function normalizeWebBaseUrl(value: string, isProd: boolean): string {
  if (cdk.Token.isUnresolved(value)) {
    return value;
  }
  const url = new URL(value);
  if (url.pathname !== "/" || url.search || url.hash) {
    throw new Error("webBaseUrl must be an origin without path, query, or fragment");
  }
  if (isProd && url.protocol !== "https:") {
    throw new Error("prod webBaseUrl must use https");
  }
  return url.origin;
}
