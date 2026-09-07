export type ConversationManagerObservation = {
  name: string;
  parameters: Record<string, unknown>;
};

function object(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

function observation(value: unknown): ConversationManagerObservation | null {
  const node = object(value);
  const parameters = object(node?.parameters);
  if (typeof node?.name !== "string" || !node.name || parameters === null) {
    return null;
  }
  return { name: node.name, parameters };
}

export function conversationManagerFromVerifyReport(
  report: unknown,
): ConversationManagerObservation | null {
  return observation(object(report)?.conversation_manager);
}

export function conversationManagerFromDescriptor(
  descriptors: unknown,
): ConversationManagerObservation | null {
  const agent = object(object(descriptors)?.agent);
  const manager = object(agent?.conversationManager);
  return observation(manager?.actual);
}

export function conversationManagerLabel(
  manager: ConversationManagerObservation,
): string {
  if (manager.name === "SlidingWindowConversationManager") {
    return `Sliding window (${String(manager.parameters.window_size ?? "?")})`;
  }
  if (manager.name === "NullConversationManager") {
    return "관리 안 함";
  }
  return manager.name;
}
