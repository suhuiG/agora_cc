#!/usr/bin/env node
import { execFileSync } from "node:child_process";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import {
  CognitoDomainPurpose,
  cognitoUserPoolName,
} from "../lib/cognito-domain";

const COGNITO_DOMAIN_PURPOSES: readonly CognitoDomainPurpose[] = [
  "human",
  "m2-oauth",
  "mcp-gateway",
];

export interface SynthesizedCognitoDomain {
  readonly stackName: string;
  readonly logicalId: string;
  readonly poolName: string;
  readonly domain: string;
  readonly purpose: CognitoDomainPurpose;
  readonly stage: string;
}

export interface LiveUserPool {
  readonly name: string;
  readonly domain?: string;
}

export type LiveUserPoolInventory =
  | {
      readonly status: "complete";
      readonly pools: readonly LiveUserPool[];
    }
  | {
      readonly status: "unknown";
      readonly reason: string;
    };

export type CognitoDomainCheck =
  | {
      readonly status: "match";
      readonly synthesized: SynthesizedCognitoDomain;
      readonly liveDomain: string;
    }
  | {
      readonly status: "not_applicable" | "domain_missing";
      readonly synthesized: SynthesizedCognitoDomain;
    }
  | {
      readonly status: "drift";
      readonly synthesized: SynthesizedCognitoDomain;
      readonly liveDomain: string;
      readonly contextArgument: string;
    };

type LiveUserPoolLookup = () => Promise<LiveUserPoolInventory>;

export async function checkCognitoDomainDrift(
  synthesizedDomains: readonly SynthesizedCognitoDomain[],
  lookupLiveUserPools: LiveUserPoolLookup,
): Promise<CognitoDomainCheck[]> {
  const inventory = await lookupLiveUserPools();
  if (inventory.status === "unknown") {
    throw new Error(
      `live Cognito user pool inventory is unknown: ${inventory.reason}`,
    );
  }
  const liveByName = new Map<string, LiveUserPool>();
  for (const pool of inventory.pools) {
    if (liveByName.has(pool.name)) {
      throw new Error(`multiple live Cognito user pools have name '${pool.name}'`);
    }
    liveByName.set(pool.name, pool);
  }

  const checks: CognitoDomainCheck[] = [];
  for (const synthesized of synthesizedDomains) {
    const live = liveByName.get(synthesized.poolName);
    if (!live) {
      checks.push({ status: "not_applicable", synthesized });
      continue;
    }
    if (!live.domain) {
      checks.push({ status: "domain_missing", synthesized });
      continue;
    }
    if (live.domain === synthesized.domain) {
      checks.push({
        status: "match",
        synthesized,
        liveDomain: live.domain,
      });
      continue;
    }
    checks.push({
      status: "drift",
      synthesized,
      liveDomain: live.domain,
      contextArgument:
        `-c cognitoDomainPrefix:${synthesized.purpose}:` +
        `${synthesized.stage}=${live.domain}`,
    });
  }
  return checks;
}

interface CloudFormationResource {
  readonly Type: string;
  readonly Properties?: Record<string, unknown>;
}

interface CloudFormationTemplate {
  readonly Resources?: Record<string, CloudFormationResource>;
}

function resolveString(
  value: unknown,
  account: string,
  region: string,
): string {
  if (typeof value === "string") {
    return value;
  }
  if (typeof value !== "object" || value === null) {
    throw new Error(`cannot resolve CloudFormation string: ${JSON.stringify(value)}`);
  }
  const intrinsic = value as Record<string, unknown>;
  if (typeof intrinsic.Ref === "string") {
    if (intrinsic.Ref === "AWS::AccountId") return account;
    if (intrinsic.Ref === "AWS::Region") return region;
  }
  const join = intrinsic["Fn::Join"];
  if (
    Array.isArray(join) &&
    join.length === 2 &&
    typeof join[0] === "string" &&
    Array.isArray(join[1])
  ) {
    return join[1]
      .map((part) => resolveString(part, account, region))
      .join(join[0]);
  }
  throw new Error(
    `unsupported CloudFormation string expression: ${JSON.stringify(value)}`,
  );
}

function purposeFromPoolName(
  poolName: string,
  stage: string,
): CognitoDomainPurpose {
  const purpose = COGNITO_DOMAIN_PURPOSES.find(
    (candidate) => cognitoUserPoolName(stage, candidate) === poolName,
  );
  if (!purpose) {
    throw new Error(
      `cannot determine Cognito domain purpose from user pool '${poolName}'`,
    );
  }
  return purpose;
}

export function collectSynthesizedDomains(
  templates: ReadonlyMap<string, CloudFormationTemplate>,
  stage: string,
  account: string,
  region: string,
): SynthesizedCognitoDomain[] {
  const domains: SynthesizedCognitoDomain[] = [];
  for (const [stackName, template] of templates) {
    const resources = template.Resources ?? {};
    for (const [logicalId, resource] of Object.entries(resources)) {
      if (resource.Type !== "AWS::Cognito::UserPoolDomain") continue;
      const userPoolId = resource.Properties?.UserPoolId as
        | { Ref?: unknown }
        | undefined;
      if (typeof userPoolId?.Ref !== "string") {
        throw new Error(
          `${stackName}/${logicalId} does not reference a user pool in its stack`,
        );
      }
      const pool = resources[userPoolId.Ref];
      const poolNameValue = pool?.Properties?.UserPoolName;
      if (pool?.Type !== "AWS::Cognito::UserPool") {
        throw new Error(
          `${stackName}/${logicalId} references unknown user pool ${userPoolId.Ref}`,
        );
      }
      const poolName = resolveString(poolNameValue, account, region);
      domains.push({
        stackName,
        logicalId,
        poolName,
        domain: resolveString(resource.Properties?.Domain, account, region),
        purpose: purposeFromPoolName(poolName, stage),
        stage,
      });
    }
  }
  return domains;
}

export interface CliOptions {
  readonly stage: string;
  readonly region: string;
  readonly profile?: string;
  readonly contexts: readonly string[];
}

function parseArgs(argv: readonly string[]): CliOptions {
  let stage = "";
  let region = process.env.AGORA_DEPLOY_REGION ?? "ap-northeast-2";
  let profile: string | undefined;
  const contexts: string[] = [];
  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index];
    const value = argv[index + 1];
    if (arg === "--stage" && value) {
      stage = value;
      index += 1;
    } else if (arg === "--region" && value) {
      region = value;
      index += 1;
    } else if (arg === "--profile" && value) {
      profile = value;
      index += 1;
    } else if ((arg === "-c" || arg === "--context") && value) {
      contexts.push(value);
      index += 1;
    } else {
      throw new Error(`unknown or incomplete argument: ${arg}`);
    }
  }
  if (!stage) throw new Error("--stage is required");
  return { stage, region, profile, contexts };
}

type AwsJson = (
  args: readonly string[],
  options: CliOptions,
) => Record<string, unknown>;

function awsJson(
  args: readonly string[],
  options: CliOptions,
): Record<string, unknown> {
  const profileArgs = options.profile ? ["--profile", options.profile] : [];
  const output = execFileSync(
    "aws",
    [...args, "--region", options.region, ...profileArgs, "--output", "json"],
    { encoding: "utf8" },
  );
  return JSON.parse(output) as Record<string, unknown>;
}

function identifyAccount(options: CliOptions): string {
  const identity = awsJson(["sts", "get-caller-identity"], options);
  if (typeof identity.Account !== "string") {
    throw new Error("aws sts get-caller-identity returned no account");
  }
  return identity.Account;
}

function synthesize(
  options: CliOptions,
  account: string,
  outdir: string,
): void {
  const contextArgs = options.contexts.flatMap((context) => ["-c", context]);
  const profileArgs = options.profile ? ["--profile", options.profile] : [];
  execFileSync(
    "npx",
    [
      "cdk",
      "synth",
      "-c",
      `stage=${options.stage}`,
      ...contextArgs,
      ...profileArgs,
      "--output",
      outdir,
      "--quiet",
    ],
    {
      env: {
        ...process.env,
        ...(options.profile ? { AWS_PROFILE: options.profile } : {}),
        AGORA_REGISTRY_ID:
          process.env.AGORA_REGISTRY_ID ?? "cognito-domain-check",
        CDK_DEFAULT_ACCOUNT: account,
        CDK_DEFAULT_REGION: options.region,
      },
      stdio: "inherit",
    },
  );
}

export interface AssemblyTemplates {
  readonly templates: Map<string, CloudFormationTemplate>;
  readonly skippedArtifacts: string[];
}

export function readAssemblyTemplates(outdir: string): AssemblyTemplates {
  const manifest = JSON.parse(
    fs.readFileSync(path.join(outdir, "manifest.json"), "utf8"),
  ) as {
    artifacts?: Record<string, {
      type?: string;
      properties?: { stackName?: string; templateFile?: string };
    }>;
  };
  const templates = new Map<string, CloudFormationTemplate>();
  const skippedArtifacts: string[] = [];
  const artifacts = Object.entries(manifest.artifacts ?? {})
    .sort(([left], [right]) => left.localeCompare(right));
  for (const [artifactId, artifact] of artifacts) {
    if (artifact.type !== "aws:cloudformation:stack") continue;
    const templateFile = artifact.properties?.templateFile;
    if (!templateFile) {
      skippedArtifacts.push(
        `${artifactId}: missing properties.templateFile`,
      );
      continue;
    }
    const stackName = artifact.properties?.stackName ?? artifactId;
    templates.set(
      stackName,
      JSON.parse(
        fs.readFileSync(path.join(outdir, templateFile), "utf8"),
      ) as CloudFormationTemplate,
    );
  }
  return { templates, skippedArtifacts };
}

export async function lookupLiveUserPools(
  options: CliOptions,
  fetchJson: AwsJson = awsJson,
): Promise<LiveUserPoolInventory> {
  try {
    const summaries: unknown[] = [];
    const seenTokens = new Set<string>();
    let nextToken: string | undefined;
    do {
      const args = [
        "cognito-idp",
        "list-user-pools",
        "--max-results",
        "60",
        "--no-paginate",
      ];
      if (nextToken) args.push("--next-token", nextToken);
      const page = fetchJson(args, options);
      if (Array.isArray(page.UserPools)) summaries.push(...page.UserPools);
      nextToken = typeof page.NextToken === "string"
        ? page.NextToken
        : undefined;
      if (nextToken) {
        if (seenTokens.has(nextToken)) {
          throw new Error(`list-user-pools repeated NextToken '${nextToken}'`);
        }
        seenTokens.add(nextToken);
      }
    } while (nextToken);

    const pools = summaries.map((summary) => {
      const id = (summary as { Id?: unknown }).Id;
      if (typeof id !== "string") {
        throw new Error("list-user-pools returned an entry without Id");
      }
      const described = fetchJson(
        ["cognito-idp", "describe-user-pool", "--user-pool-id", id],
        options,
      );
      const pool = described.UserPool as
        | { Name?: unknown; Domain?: unknown }
        | undefined;
      if (typeof pool?.Name !== "string") {
        throw new Error(`describe-user-pool returned no Name for ${id}`);
      }
      return {
        name: pool.Name,
        domain: typeof pool.Domain === "string" ? pool.Domain : undefined,
      };
    });
    return { status: "complete", pools };
  } catch (error: unknown) {
    return {
      status: "unknown",
      reason: error instanceof Error ? error.message : String(error),
    };
  }
}

export async function main(argv: readonly string[]): Promise<number> {
  const options = parseArgs(argv);
  const account = identifyAccount(options);
  const outdir = fs.mkdtempSync(path.join(os.tmpdir(), "agora-cognito-check-"));
  let synthesized: SynthesizedCognitoDomain[];
  try {
    synthesize(options, account, outdir);
    const assembly = readAssemblyTemplates(outdir);
    for (const skipped of assembly.skippedArtifacts) {
      console.warn(`Skipped CloudFormation stack artifact: ${skipped}`);
    }
    synthesized = collectSynthesizedDomains(
      assembly.templates,
      options.stage,
      account,
      options.region,
    );
    if (synthesized.length === 0) {
      const stackNames = [...assembly.templates.keys()];
      throw new Error(
        "assembly를 읽었지만 UserPoolDomain 리소스를 찾지 못했어요" +
        "(파싱 실패 가능). " +
        `읽은 스택 ${stackNames.length}개: ` +
        `${stackNames.length > 0 ? stackNames.join(", ") : "(없음)"}`,
      );
    }
  } finally {
    fs.rmSync(outdir, { recursive: true, force: true });
  }
  const checks = await checkCognitoDomainDrift(
    synthesized,
    () => lookupLiveUserPools(options),
  );
  const failures = checks.filter(
    (check) => check.status === "drift" || check.status === "domain_missing",
  );
  if (failures.length === 0) {
    console.log("Cognito domain check passed.");
    return 0;
  }

  for (const issue of failures) {
    const subject = issue.synthesized;
    if (issue.status === "domain_missing") {
      console.error(
        `${subject.stackName}/${subject.logicalId}: live pool ` +
        `${subject.poolName} has no Cognito domain; refusing to infer safety`,
      );
      continue;
    }
    if (issue.status !== "drift") continue;
    console.error("이 배포는 도메인을 교체해요. 다음 context를 주입하세요:");
    console.error(
      `${subject.stackName}/${subject.logicalId}: pool=${subject.poolName}, ` +
      `synth=${subject.domain}, live=${issue.liveDomain}`,
    );
    console.error(issue.contextArgument);
  }
  return 1;
}

if (require.main === module) {
  main(process.argv.slice(2))
    .then((code) => {
      process.exitCode = code;
    })
    .catch((error: unknown) => {
      console.error(error instanceof Error ? error.message : String(error));
      process.exitCode = 1;
    });
}
