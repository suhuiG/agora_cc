export type OwnerDirectoryUser = {
  sub: string;
  name: string;
  email: string;
};

export type OwnerResolution =
  | { state: "loading" }
  | { state: "unknown" }
  | { state: "resolved"; user: OwnerDirectoryUser };

export function isEmailOwnerId(ownerId: string): boolean {
  return /^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(ownerId.trim());
}

export function ownerPresentation(
  ownerId: string,
  resolution: OwnerResolution,
): {
  label: string;
  detail: string;
  state: OwnerResolution["state"] | "readable";
} {
  if (isEmailOwnerId(ownerId)) {
    return {
      label: ownerId,
      detail: "이메일 등록자",
      state: "readable",
    };
  }
  if (resolution.state === "resolved") {
    const name = resolution.user.name.trim();
    const email = resolution.user.email.trim();
    return {
      label: name || email || ownerId || "—",
      detail: name && email ? email : ownerId,
      state: "resolved",
    };
  }
  if (resolution.state === "loading") {
    return {
      label: ownerId || "—",
      detail: "사용자 디렉터리 해석 중",
      state: "loading",
    };
  }
  return {
    label: ownerId || "—",
    detail: "미해석 · 사용자 디렉터리에서 확인할 수 없음",
    state: "unknown",
  };
}

export type ScanApplicability = {
  state: "applicable" | "not_applicable" | "unknown";
  reason: string;
};

export function scanApplicabilityPresentation(
  applicability: ScanApplicability,
): { label: string; state: ScanApplicability["state"] } | null {
  if (applicability.state === "applicable") return null;
  return {
    label: applicability.reason || (
      applicability.state === "not_applicable"
        ? "소스 스캔 대상 아님"
        : "스캔 적용 여부 미해석"
    ),
    state: applicability.state,
  };
}

export function scanRunAvailability(
  applicability: ScanApplicability,
): { enabled: boolean; reason: string } {
  if (applicability.state === "not_applicable") {
    return {
      enabled: false,
      reason: applicability.reason || "소스 스캔 대상 아님",
    };
  }
  return { enabled: true, reason: "" };
}
