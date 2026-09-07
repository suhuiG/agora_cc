import type {
  AccessCapability,
  AccessCapabilityList,
} from "./api/identity";

export function accessCapabilityCacheKey(
  connectionId: string,
): string {
  return `admin/access/connections/${connectionId}/capabilities?shape=versioned-list`;
}

export function accessCapabilityItems(
  value: unknown,
): AccessCapability[] {
  if (Array.isArray(value)) return value;
  if (!value || typeof value !== "object" || !("items" in value)) return [];
  const items = (value as AccessCapabilityList).items;
  return Array.isArray(items) ? items : [];
}
