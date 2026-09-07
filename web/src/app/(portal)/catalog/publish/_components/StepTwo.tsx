import type { ChangeEventHandler } from "react";

import type { AgentConnectTestResult, SourceFile } from "@/lib/api";

import { AgentDeployModeStep } from "./AgentDeployModeStep";
import { AgentDomainModeStep } from "./AgentDomainModeStep";
import { AgentJsonModeStep } from "./AgentJsonModeStep";
import { McpConnectModeStep } from "./McpConnectModeStep";
import { McpDeployModeStep } from "./McpDeployModeStep";
import { SourceModeStep } from "./SourceModeStep";
import type { FileRow, WizardChoice } from "./types";

type StepTwoProps = {
  choice: WizardChoice;
  files: FileRow[];
  skippedFiles: string[];
  excludedSecrets: string[];
  mcpEndpoint: string;
  connectVerified: boolean;
  connecting: boolean;
  mcpTools: { name: string; description: string }[];
  deployFiles: SourceFile[];
  agentDeployFiles: SourceFile[];
  agentCardText: string;
  agentCardValid: boolean;
  agentDomain: string;
  agentConnecting: boolean;
  agentCard: AgentConnectTestResult | null;
  agentDomainError: string;
  canContinue: boolean;
  onPickFolder: ChangeEventHandler<HTMLInputElement>;
  onPickFile: ChangeEventHandler<HTMLInputElement>;
  onUpdateFile: (
    id: string,
    patch: Partial<Pick<FileRow, "path" | "content">>,
  ) => void;
  onAddFile: () => void;
  onRemoveFile: (id: string) => void;
  onMcpEndpointChange: (value: string) => void;
  onMcpConnect: () => void;
  onPickDeployFolder: ChangeEventHandler<HTMLInputElement>;
  onPickAgentDeployFolder: ChangeEventHandler<HTMLInputElement>;
  onAgentCardTextChange: (value: string) => void;
  onPickAgentCard: ChangeEventHandler<HTMLInputElement>;
  onAgentDomainChange: (value: string) => void;
  onAgentConnect: () => void;
  onPrevious: () => void;
  onNext: () => void;
};

export function StepTwo({
  choice,
  files,
  skippedFiles,
  excludedSecrets,
  mcpEndpoint,
  connectVerified,
  connecting,
  mcpTools,
  deployFiles,
  agentDeployFiles,
  agentCardText,
  agentCardValid,
  agentDomain,
  agentConnecting,
  agentCard,
  agentDomainError,
  canContinue,
  onPickFolder,
  onPickFile,
  onUpdateFile,
  onAddFile,
  onRemoveFile,
  onMcpEndpointChange,
  onMcpConnect,
  onPickDeployFolder,
  onPickAgentDeployFolder,
  onAgentCardTextChange,
  onPickAgentCard,
  onAgentDomainChange,
  onAgentConnect,
  onPrevious,
  onNext,
}: StepTwoProps) {
  return (
    <div>
      {choice.mode === "source" ? (
        <SourceModeStep
          choice={choice}
          files={files}
          skippedFiles={skippedFiles}
          excludedSecrets={excludedSecrets}
          onPickFolder={onPickFolder}
          onPickFile={onPickFile}
          onUpdateFile={onUpdateFile}
          onAddFile={onAddFile}
          onRemoveFile={onRemoveFile}
        />
      ) : choice.mode === "reference" ? (
        <McpConnectModeStep
          endpoint={mcpEndpoint}
          connecting={connecting}
          connectVerified={connectVerified}
          tools={mcpTools}
          onEndpointChange={onMcpEndpointChange}
          onConnect={onMcpConnect}
        />
      ) : choice.mode === "deploy" ? (
        <McpDeployModeStep
          files={deployFiles}
          skippedFiles={skippedFiles}
          excludedSecrets={excludedSecrets}
          onPickFolder={onPickDeployFolder}
        />
      ) : choice.mode === "agent-deploy" ? (
        <AgentDeployModeStep
          files={agentDeployFiles}
          skippedFiles={skippedFiles}
          excludedSecrets={excludedSecrets}
          onPickFolder={onPickAgentDeployFolder}
        />
      ) : choice.mode === "agent-json" ? (
        <AgentJsonModeStep
          cardText={agentCardText}
          cardValid={agentCardValid}
          onCardTextChange={onAgentCardTextChange}
          onPickCard={onPickAgentCard}
        />
      ) : (
        <AgentDomainModeStep
          domain={agentDomain}
          connecting={agentConnecting}
          card={agentCard}
          error={agentDomainError}
          onDomainChange={onAgentDomainChange}
          onConnect={onAgentConnect}
        />
      )}

      <div className="flex gap-3 mt-6">
        <button
          type="button"
          onClick={onPrevious}
          className="px-4 py-2 border border-slate-300 rounded-lg hover:bg-slate-50"
        >
          이전
        </button>
        <button
          type="button"
          onClick={onNext}
          disabled={!canContinue}
          className="px-4 py-2 bg-slate-900 text-white rounded-lg hover:bg-slate-700 disabled:opacity-40 disabled:cursor-not-allowed"
        >
          다음
        </button>
      </div>
    </div>
  );
}
