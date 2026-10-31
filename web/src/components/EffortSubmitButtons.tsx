import { Button } from "@/components/ui/button";

/** One-click effort selection and submission for supported levels. */
export function EffortSubmitButtons({
  levels,
  disabled,
  onSubmit,
}: {
  levels: readonly string[];
  disabled: boolean;
  onSubmit: (effort: string) => void;
}) {
  return (
    <div className="flex shrink-0 gap-1">
      {["low", "medium", "high", "xhigh"]
        .filter((level) => levels.includes(level))
        .map((level) => (
          <Button
            key={level}
            type="button"
            variant="outline"
            size="sm"
            className="h-9 px-2 text-xs md:h-8"
            disabled={disabled}
            aria-label={`Send with ${level} effort`}
            title={`Send with ${level} effort`}
            onClick={() => onSubmit(level)}
          >
            {level === "xhigh" ? "X-high" : level.charAt(0).toUpperCase() + level.slice(1)}
          </Button>
        ))}
    </div>
  );
}
