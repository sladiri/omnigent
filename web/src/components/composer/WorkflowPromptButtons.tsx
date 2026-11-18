import { WorkflowIcon } from "lucide-react";
import { useState } from "react";
import { COMPOSER_CONTENT_INSET_CLASS } from "@/components/composer/ChatComposer";
import { Button } from "@/components/ui/button";

export function WorkflowPromptButtons({
  agentName,
  disabled,
  draft,
  onChange,
  coordinator,
}: {
  agentName: string | null | undefined;
  disabled: boolean;
  draft: string;
  onChange: (draft: string) => void;
  coordinator?: {
    harness: "codex-native" | "claude-native";
    onChange: (harness: "codex-native" | "claude-native") => void;
  };
}) {
  const [pickedProvider, setPickedProvider] = useState("codex");
  if (agentName !== "workflow-controller") return null;
  const prefix =
    /^Prepare the start message for `(review-fix|implement-review-fix|compare-review-fix)` using the task below\.(?:\r?\n)*/;
  const selected = draft.match(prefix)?.[1];
  const providerRequest = /^Use workflow_draft provider="(codex|claude)"\.(?:\r?\n)*/;
  const provider =
    (selected ? draft.replace(prefix, "").match(providerRequest)?.[1] : null) ?? pickedProvider;
  let task = draft;
  while (prefix.test(task)) task = task.replace(prefix, "").replace(providerRequest, "");
  const prepare = (prompt: string, choice: string) =>
    onChange(`${prompt}\n\nUse workflow_draft provider="${choice}".\n\n${task}`);
  return (
    <div
      className={`${COMPOSER_CONTENT_INSET_CLASS} flex flex-wrap items-center gap-2 pb-2`}
      role="group"
      aria-label="Workflow-controller workflows"
    >
      <span className="w-full text-xs text-muted-foreground">Workflow-controller</span>
      {coordinator && (
        <label className="flex items-center gap-2 text-xs">
          Coordinator
          <select
            aria-label="Coordinator"
            className="h-9 rounded-md border border-input bg-background px-2 md:h-8"
            value={coordinator.harness}
            disabled={disabled}
            onChange={(event) =>
              coordinator.onChange(
                event.target.value === "claude-native" ? "claude-native" : "codex-native",
              )
            }
          >
            <option value="codex-native">Codex</option>
            <option value="claude-native">Claude</option>
          </select>
        </label>
      )}
      <label className="flex items-center gap-2 text-xs">
        Worker provider
        <select
          aria-label="Worker provider"
          className="h-9 rounded-md border border-input bg-background px-2 md:h-8"
          value={provider}
          disabled={disabled}
          onChange={(event) => {
            const choice = event.target.value;
            setPickedProvider(choice);
            if (selected)
              prepare(
                `Prepare the start message for \`${selected}\` using the task below.`,
                choice,
              );
          }}
        >
          <option value="codex">Codex</option>
          <option value="claude">Claude</option>
        </select>
      </label>
      {[
        {
          label: "review",
          prompt: "Prepare the start message for `review-fix` using the task below.",
        },
        {
          label: "implement",
          prompt: "Prepare the start message for `implement-review-fix` using the task below.",
        },
        {
          label: "compare",
          prompt: "Prepare the start message for `compare-review-fix` using the task below.",
        },
      ].map(({ label, prompt }) => (
        <Button
          key={label}
          type="button"
          variant={draft.startsWith(prompt) ? "default" : "outline"}
          size="sm"
          className="h-9 px-2 text-xs md:h-8"
          title={`Prepare ${label} workflow`}
          data-workflow-prompt=""
          aria-label={label}
          aria-pressed={draft.startsWith(prompt)}
          disabled={disabled}
          onClick={() => prepare(prompt, provider)}
        >
          <WorkflowIcon className="size-4" aria-hidden="true" />
          <span>{label}</span>
        </Button>
      ))}
      <span className="w-full text-xs text-muted-foreground" role="status">
        {selected
          ? `${selected.split("-")[0]} selected · Send to prepare`
          : "Choose a workflow, then send your task"}
      </span>
    </div>
  );
}
