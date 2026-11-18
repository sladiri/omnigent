import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { QuickModelChoice } from "./QuickModelChoice";

afterEach(cleanup);

const options = [
  { id: "sol", model: "system.ai.gpt-6-2-sol" },
  { id: "astra", model: "gpt-6.2-astra" },
  { id: "terra", model: "system.ai.gpt-6-1-terra" },
];

describe("QuickModelChoice", () => {
  it("selects advertised IDs and highlights only the reported model", () => {
    const onSelect = vi.fn();
    const props = { options, currentModel: "databricks-gpt-6-2-sol", disabled: false, onSelect };
    const { rerender } = render(<QuickModelChoice kind="codex" {...props} />);
    const med = screen.getByRole("button", { name: "Med: GPT-6.2 Sol" });
    const hi = screen.getByRole("button", { name: "Hi: GPT-6.2 Astra" });
    const lo = screen.getByRole("button", { name: "Lo: GPT-6.1 Terra" });
    expect(screen.getAllByRole("button")).toEqual([lo, med, hi]);
    expect(med).toHaveAttribute("aria-pressed", "true");
    fireEvent.click(med);
    expect(onSelect).not.toHaveBeenCalled();
    fireEvent.click(hi);
    expect(onSelect).toHaveBeenCalledWith("astra");
    expect(hi).toHaveAttribute("aria-pressed", "false");
    rerender(<QuickModelChoice kind="codex" {...props} currentModel="gpt-6.2-astra" />);
    expect(hi).toHaveAttribute("aria-pressed", "true");
    fireEvent.click(med);
    expect(onSelect).toHaveBeenLastCalledWith("sol");
    fireEvent.click(lo);
    expect(onSelect).toHaveBeenLastCalledWith("terra");
    rerender(<QuickModelChoice kind="codex" {...props} currentModel="gpt-6.1-terra" />);
    expect(lo).toHaveAttribute("aria-pressed", "true");
    rerender(<QuickModelChoice kind="codex" {...props} currentModel="gpt-6-luna" />);
    expect(med).toHaveAttribute("aria-pressed", "false");
    expect(hi).toHaveAttribute("aria-pressed", "false");
  });

  it.each(
    [
      [],
      [options[0]],
      options.slice(0, 2),
      [...options, { id: "gpt-6.2-sol" }],
      [...options, { id: "other-astra", model: "gpt-6.2-astra" }],
      [{ id: "gpt-6.2-sol", model: "gpt-6.2-astra" }],
      [{ id: "unknown", displayName: "GPT-6.2 Sol" }, options[1]],
    ].map((catalog) => ({ catalog })),
  )("hides missing or ambiguous mappings: $catalog", ({ catalog }) => {
    render(
      <QuickModelChoice
        kind="codex"
        options={catalog}
        currentModel={null}
        disabled={false}
        onSelect={vi.fn()}
      />,
    );
    expect(screen.queryByRole("group")).toBeNull();
  });

  it("keeps Sonnet and Opus available when the Claude catalog omits Fable", () => {
    render(
      <QuickModelChoice
        kind="claude"
        options={[{ id: "sonnet" }, { id: "opus" }]}
        currentModel={null}
        disabled={false}
        onSelect={vi.fn()}
      />,
    );
    expect(screen.getByRole("button", { name: "Lo: Sonnet" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Med: Opus" })).toBeEnabled();
    expect(screen.queryByRole("button", { name: "Hi: Fable" })).toBeNull();
  });

  it("blocks model changes when disabled", () => {
    const onSelect = vi.fn();
    render(
      <QuickModelChoice
        kind="codex"
        options={options}
        currentModel={null}
        disabled
        onSelect={onSelect}
      />,
    );
    for (const button of screen.getAllByRole("button")) {
      expect(button).toBeDisabled();
      fireEvent.click(button);
    }
    expect(onSelect).not.toHaveBeenCalled();
  });
});
