export const SEMVER_RE = /^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$/;

export function incrementSemver(
  version: string,
  part: "major" | "minor",
): string | null {
  const match = version.trim().match(SEMVER_RE);
  if (!match) return null;

  const major = Number(match[1]);
  const minor = Number(match[2]);
  return part === "major"
    ? `${major + 1}.0.0`
    : `${major}.${minor + 1}.0`;
}
