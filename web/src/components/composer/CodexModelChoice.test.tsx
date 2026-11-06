import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { CodexModelChoice } from "./CodexModelChoice";

afterEach(cleanup);

const options = [
  { id: "sol", model: "system.ai.gpt-6-1-sol" },
  { id: "astra", model: "gpt-6-astra" },
];

describe("CodexModelChoice", () => {
  it("selects advertised IDs and highlights only the reported model", () => {
    const onSelect = vi.fn();
    const props = { options, currentModel: "databricks-gpt-6-1-sol", disabled: false, onSelect };
    const { rerender } = render(<CodexModelChoice {...props} />);
    const med = screen.getByRole("button", { name: "Med: GPT-6.1 Sol" });
    const hi = screen.getByRole("button", { name: "Hi: GPT-6 Astra" });
    expect(med).toHaveAttribute("aria-pressed", "true");
    fireEvent.click(med);
    expect(onSelect).not.toHaveBeenCalled();
    fireEvent.click(hi);
    expect(onSelect).toHaveBeenCalledWith("astra");
    expect(hi).toHaveAttribute("aria-pressed", "false");
    rerender(<CodexModelChoice {...props} currentModel="gpt-6-astra" />);
    expect(hi).toHaveAttribute("aria-pressed", "true");
    fireEvent.click(med);
    expect(onSelect).toHaveBeenLastCalledWith("sol");
    rerender(<CodexModelChoice {...props} currentModel="gpt-6-luna" />);
    expect(med).toHaveAttribute("aria-pressed", "false");
    expect(hi).toHaveAttribute("aria-pressed", "false");
  });

  it.each(
    [
      [],
      [options[0]],
      [...options, { id: "gpt-6.1-sol" }],
      [...options, { id: "other-astra", model: "gpt-6-astra" }],
      [{ id: "gpt-6.1-sol", model: "gpt-6-astra" }],
      [{ id: "unknown", displayName: "GPT-6.1 Sol" }, options[1]],
    ].map((catalog) => ({ catalog })),
  )("hides missing or ambiguous mappings: $catalog", ({ catalog }) => {
    render(
      <CodexModelChoice
        options={catalog}
        currentModel={null}
        disabled={false}
        onSelect={vi.fn()}
      />,
    );
    expect(screen.queryByRole("group")).toBeNull();
  });

  it("blocks model changes when disabled", () => {
    const onSelect = vi.fn();
    render(<CodexModelChoice options={options} currentModel={null} disabled onSelect={onSelect} />);
    for (const button of screen.getAllByRole("button")) {
      expect(button).toBeDisabled();
      fireEvent.click(button);
    }
    expect(onSelect).not.toHaveBeenCalled();
  });
});
