import { WorkflowIcon } from "lucide-react";
import { COMPOSER_CONTENT_INSET_CLASS } from "@/components/composer/ChatComposer";
import { Button } from "@/components/ui/button";

export function WorkflowPromptButtons({
  agentName,
  disabled,
  draft,
  onChange,
}: {
  agentName: string | null | undefined;
  disabled: boolean;
  draft: string;
  onChange: (draft: string) => void;
}) {
  if (agentName !== "workflow-controller") return null;
  const prefix =
    /^Prepare the start message for `(review-fix|implement-review-fix|compare-review-fix)` using the task below\.(?:\r?\n)*/;
  const selected = draft.match(prefix)?.[1];
  let task = draft;
  while (prefix.test(task)) task = task.replace(prefix, "");
  return (
    <div
      className={`${COMPOSER_CONTENT_INSET_CLASS} flex flex-wrap items-center gap-2 pb-2`}
      role="group"
      aria-label="Workflow-controller workflows"
    >
      <span className="w-full text-xs text-muted-foreground">Workflow-controller</span>
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
          onClick={() => onChange(`${prompt}\n\n${task}`)}
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
