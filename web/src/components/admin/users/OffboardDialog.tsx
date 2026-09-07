"use client";

import { useState } from "react";

import { Button } from "@/components/ui/button";
import { Modal } from "@/components/ui/modal";
import { disableCognitoUser, type CognitoUser } from "@/lib/api";

export function OffboardDialog({
  user,
  onClose,
  onDisabled,
}: {
  user: CognitoUser | null;
  onClose: () => void;
  onDisabled: (revokedGrants: number) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function disable() {
    if (!user) return;
    setBusy(true);
    setError("");
    try {
      const result = await disableCognitoUser(user.sub);
      onDisabled(result.revoked_grants);
      onClose();
    } catch (caught) {
      setError(errorMessage(caught, "오프보딩에 실패했어요."));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal
      open={Boolean(user)}
      title="사용자 오프보딩"
      description={
        user
          ? `${user.name || user.email} 계정을 비활성화하고 모든 활성 grant를 즉시 회수해요.`
          : undefined
      }
      busy={busy}
      onClose={onClose}
      footer={
        <>
          <Button type="button" variant="outline" onClick={onClose} disabled={busy}>
            취소
          </Button>
          <Button type="button" variant="destructive" onClick={disable} disabled={busy}>
            {busy ? "비활성화 중..." : "비활성화 및 grant 회수"}
          </Button>
        </>
      }
    >
      <p className="text-sm text-muted-foreground">
        사용자는 더 이상 로그인할 수 없어요. 계정은 감사와 복구를 위해 삭제하지 않아요.
      </p>
      {error && <p className="mt-3 text-sm text-red-700">{error}</p>}
    </Modal>
  );
}

function errorMessage(error: unknown, fallback: string): string {
  return error instanceof Error && error.message ? error.message : fallback;
}
