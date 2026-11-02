import { WorkflowIcon } from "lucide-react";
import { COMPOSER_CONTENT_INSET_CLASS } from "@/components/composer/ChatComposer";
import { Button } from "@/components/ui/button";

export function WorkflowPromptButtons({
  agentName,
  disabled,
  onInsert,
}: {
  agentName: string | null | undefined;
  disabled: boolean;
  onInsert: (prompt: string) => void;
}) {
  if (agentName !== "workflow-controller") return null;
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
          variant="outline"
          size="sm"
          className="h-9 px-2 text-xs md:h-8"
          title={label}
          data-workflow-prompt=""
          aria-label={label}
          disabled={disabled}
          onClick={() => onInsert(prompt)}
        >
          <WorkflowIcon className="size-4" aria-hidden="true" />
          <span>{label}</span>
        </Button>
      ))}
    </div>
  );
}
