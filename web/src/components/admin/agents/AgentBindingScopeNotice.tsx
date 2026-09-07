import Link from "next/link";

import { AGENT_BINDING_SCOPE_NOTICE } from "@/lib/agentBindingScope";

export function AgentBindingScopeNotice() {
  return (
    <div
      role="note"
      className="mb-4 rounded-md border border-blue-200 bg-blue-50 px-3 py-2 text-sm text-blue-950"
    >
      <p>
        <strong>{AGENT_BINDING_SCOPE_NOTICE.title}</strong>{" "}
        {AGENT_BINDING_SCOPE_NOTICE.body}
      </p>
      <p className="mt-1 text-xs text-blue-900">
        {AGENT_BINDING_SCOPE_NOTICE.followUp}{" "}
        <Link
          href={AGENT_BINDING_SCOPE_NOTICE.href}
          className="font-semibold text-primary hover:underline"
        >
          {AGENT_BINDING_SCOPE_NOTICE.linkLabel}
        </Link>
      </p>
    </div>
  );
}
