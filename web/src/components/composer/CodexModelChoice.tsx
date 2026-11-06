import { Button } from "@/components/ui/button";
import { findNativeModelOption } from "@/lib/codexNativeModels";
import type { NativeModelOption } from "@/lib/types";

const MODEL_CHOICES = [
  { label: "Med", model: "gpt-6.1-sol", name: "GPT-6.1 Sol" },
  { label: "Hi", model: "gpt-6-astra", name: "GPT-6 Astra" },
] as const;

/** Direct model choices, shown only when the session catalog resolves each uniquely. */
export function CodexModelChoice({
  options,
  currentModel,
  disabled,
  onSelect,
}: {
  options: readonly NativeModelOption[];
  currentModel: string | null;
  disabled: boolean;
  onSelect: (model: string) => void;
}) {
  const choices = MODEL_CHOICES.map((choice) => ({
    ...choice,
    matches: options.filter((option) => findNativeModelOption([option], choice.model) !== null),
  }));
  if (choices.some((choice) => choice.matches.length !== 1)) return null;
  if (new Set(choices.map((choice) => choice.matches[0].id)).size !== choices.length) return null;

  return (
    <div role="group" aria-label="Codex model choice" className="flex shrink-0 items-center gap-1">
      <span className="text-xs text-muted-foreground">Model</span>
      <div className="flex rounded-lg border p-0.5">
        {choices.map(({ label, name, matches: [option] }) => {
          const selected = findNativeModelOption([option], currentModel) !== null;
          return (
            <Button
              key={label}
              type="button"
              variant="ghost"
              size="sm"
              className="h-7 px-2 text-xs aria-pressed:bg-muted aria-pressed:text-foreground"
              aria-label={`${label}: ${name}`}
              aria-pressed={selected}
              title={`${label}: ${name}`}
              disabled={disabled}
              onClick={() => {
                if (!selected) onSelect(option.id);
              }}
            >
              {label}
            </Button>
          );
        })}
      </div>
    </div>
  );
}
