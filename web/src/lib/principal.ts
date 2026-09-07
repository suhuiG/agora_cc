const UUID_RE =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

export type PrincipalPresentation = {
  label: string;
  title?: string;
};

export function principalPresentation(
  value: string | null | undefined,
): PrincipalPresentation {
  const principal = value?.trim() ?? "";
  if (!principal) return { label: "—" };

  return {
    label: UUID_RE.test(principal) ? `${principal.slice(0, 8)}…` : principal,
    title: principal,
  };
}
