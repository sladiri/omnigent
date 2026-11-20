import { WorkflowIcon } from "lucide-react";
import { COMPOSER_CONTENT_INSET_CLASS } from "@/components/composer/ChatComposer";
import { Button } from "@/components/ui/button";

export function WorkflowPromptButtons({
  agentName,
  disabled,
  draft,
  onChange,
  harness,
  onHarnessChange,
}: {
  agentName: string | null | undefined;
  disabled: boolean;
  draft: string;
  onChange: (draft: string) => void;
  /** The coordinator harness; workflow workers use the same provider. */
  harness: string | null | undefined;
  /** Shown before launch, when the coordinator harness can still change. */
  onHarnessChange?: (harness: "codex-native" | "claude-native") => void;
}) {
  if (agentName !== "workflow-controller") return null;
  const prefix =
    /^Prepare the start message for `(review-fix|implement-review-fix|compare-review-fix)` using the task below\.(?:\r?\n)*/;
  const selected = draft.match(prefix)?.[1];
  const providerRequest = /^Use workflow_draft provider="(codex|claude)"\.(?:\r?\n)*/;
  const provider = harness === "claude-native" ? "claude" : "codex";
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
      {onHarnessChange && (
        <label className="flex items-center gap-2 text-xs">
          Provider
          <select
            aria-label="Provider"
            className="h-9 rounded-md border border-input bg-background px-2 md:h-8"
            value={provider}
            disabled={disabled}
            onChange={(event) => {
              const choice = event.target.value === "claude" ? "claude" : "codex";
              onHarnessChange(choice === "claude" ? "claude-native" : "codex-native");
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
      )}
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
