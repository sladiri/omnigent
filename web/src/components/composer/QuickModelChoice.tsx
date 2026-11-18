import { Button } from "@/components/ui/button";
import { findNativeModelOption } from "@/lib/codexNativeModels";
import type { NativeModelOption } from "@/lib/types";

const CODEX_CHOICES = [
  { label: "Lo", model: "gpt-6.1-terra", name: "GPT-6.1 Terra" },
  { label: "Med", model: "gpt-6.2-sol", name: "GPT-6.2 Sol" },
  { label: "Hi", model: "gpt-6.2-astra", name: "GPT-6.2 Astra" },
] as const;

const CLAUDE_CHOICES = [
  { label: "Lo", model: "sonnet", name: "Sonnet" },
  { label: "Med", model: "opus", name: "Opus" },
  { label: "Hi", model: "fable", name: "Fable" },
] as const;

/** Direct model choices from the session catalog. Codex requires the full preset set. */
export function QuickModelChoice({
  kind,
  options,
  currentModel,
  disabled,
  onSelect,
}: {
  kind: "codex" | "claude";
  options: readonly NativeModelOption[];
  currentModel: string | null;
  disabled: boolean;
  onSelect: (model: string) => void;
}) {
  const presets = kind === "claude" ? CLAUDE_CHOICES : CODEX_CHOICES;
  const choices = presets
    .map((choice) => ({
      ...choice,
      matches: options.filter(
        (option) =>
          findNativeModelOption([option], choice.model) !== null ||
          // Claude can advertise a versioned picker ID instead of a family alias.
          (kind === "claude" &&
            [option.id, option.model].some((model) =>
              new RegExp(`^(?:system\\.ai\\.|databricks-)?claude-${choice.model}-\\d`).test(
                model ?? "",
              ),
            )),
      ),
    }))
    .filter((choice) => choice.matches.length === 1);
  if (choices.length === 0 || (kind === "codex" && choices.length !== presets.length)) return null;
  if (new Set(choices.map((choice) => choice.matches[0].id)).size !== choices.length) return null;

  return (
    <div
      role="group"
      aria-label={kind === "claude" ? "Claude model choice" : "Codex model choice"}
      className="flex shrink-0 items-center gap-1"
    >
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
