"use client";

import { useState } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Modal } from "@/components/ui/modal";
import { inviteCognitoUser, type CognitoUser } from "@/lib/api";

export function InviteUserDialog({
  open,
  onClose,
  onCreated,
}: {
  open: boolean;
  onClose: () => void;
  onCreated: (user: CognitoUser) => void;
}) {
  const [email, setEmail] = useState("");
  const [name, setName] = useState("");
  const [team, setTeam] = useState("");
  const [groups, setGroups] = useState<string[]>(["user"]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  function toggleGroup(group: string) {
    setGroups((current) =>
      current.includes(group)
        ? current.filter((item) => item !== group)
        : [...current, group],
    );
  }

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (groups.length === 0) {
      setError("그룹을 하나 이상 선택하세요.");
      return;
    }
    setBusy(true);
    setError("");
    try {
      const user = await inviteCognitoUser({ email, name, team, groups });
      onCreated(user);
      onClose();
    } catch (caught) {
      setError(errorMessage(caught, "사용자 초대에 실패했어요."));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal
      open={open}
      title="사용자 초대"
      description="Cognito가 임시 비밀번호와 로그인 안내를 이메일로 보내요."
      busy={busy}
      onClose={onClose}
      footer={
        <>
          <Button type="button" variant="outline" onClick={onClose} disabled={busy}>
            취소
          </Button>
          <Button type="submit" form="invite-user-form" disabled={busy}>
            {busy ? "초대 중..." : "초대"}
          </Button>
        </>
      }
    >
      <form id="invite-user-form" onSubmit={submit} className="space-y-4">
        <Field label="이메일" htmlFor="invite-email">
          <Input
            id="invite-email"
            type="email"
            required
            autoComplete="email"
            value={email}
            onChange={(event) => setEmail(event.target.value)}
          />
        </Field>
        <Field label="이름" htmlFor="invite-name">
          <Input
            id="invite-name"
            required
            autoComplete="name"
            value={name}
            onChange={(event) => setName(event.target.value)}
          />
        </Field>
        <Field label="부서" htmlFor="invite-team">
          <Input
            id="invite-team"
            value={team}
            onChange={(event) => setTeam(event.target.value)}
          />
        </Field>
        <fieldset>
          <legend className="mb-2 text-sm font-medium">그룹</legend>
          <div className="flex gap-5">
            {["user", "admin"].map((group) => (
              <label key={group} className="flex items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  checked={groups.includes(group)}
                  onChange={() => toggleGroup(group)}
                  className="size-4 accent-primary"
                />
                {group}
              </label>
            ))}
          </div>
        </fieldset>
        {error && <p className="text-sm text-red-700">{error}</p>}
      </form>
    </Modal>
  );
}

function Field({
  label,
  htmlFor,
  children,
}: {
  label: string;
  htmlFor: string;
  children: React.ReactNode;
}) {
  return (
    <label htmlFor={htmlFor} className="block">
      <span className="mb-1.5 block text-sm font-medium">{label}</span>
      {children}
    </label>
  );
}

function errorMessage(error: unknown, fallback: string): string {
  return error instanceof Error && error.message ? error.message : fallback;
}
