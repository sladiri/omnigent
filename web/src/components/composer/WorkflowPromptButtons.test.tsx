import { useState } from "react";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { WorkflowPromptButtons } from "./WorkflowPromptButtons";

afterEach(cleanup);

function Editor({ initial = "My task", disabled = false, selectable = true }) {
  const [draft, setDraft] = useState(initial);
  const [harness, setHarness] = useState("codex-native");
  return (
    <>
      <WorkflowPromptButtons
        agentName="workflow-controller"
        disabled={disabled}
        draft={draft}
        onChange={setDraft}
        harness={harness}
        onHarnessChange={selectable ? setHarness : undefined}
      />
      <output aria-label="Draft">{draft}</output>
      <output aria-label="Harness">{harness}</output>
    </>
  );
}

it("uses one provider for the coordinator and workers without duplicating the task", () => {
  render(<Editor />);
  const provider = screen.getByRole("combobox", { name: "Provider" });
  expect(provider).toHaveValue("codex");
  expect(screen.queryByRole("combobox", { name: "Worker provider" })).toBeNull();
  fireEvent.change(provider, { target: { value: "claude" } });
  expect(screen.getByLabelText("Harness")).toHaveTextContent("claude-native");
  expect(screen.getByLabelText("Draft")).toHaveTextContent("My task");
  for (const workflow of ["implement", "compare", "review"]) {
    fireEvent.click(screen.getByRole("button", { name: workflow }));
    fireEvent.click(screen.getByRole("button", { name: workflow }));
    expect(screen.getByLabelText("Draft").textContent).toBe(
      `Prepare the start message for \`${workflow === "review" ? "review-fix" : `${workflow}-review-fix`}\` using the task below.\n\nUse workflow_draft provider="claude".\n\nMy task`,
    );
  }
  fireEvent.change(provider, { target: { value: "codex" } });
  expect(screen.getByLabelText("Harness")).toHaveTextContent("codex-native");
  expect(screen.getByLabelText("Draft").textContent).toBe(
    'Prepare the start message for `review-fix` using the task below.\n\nUse workflow_draft provider="codex".\n\nMy task',
  );
});

it("disables the provider selector with the workflow controls", () => {
  render(<Editor disabled />);
  expect(screen.getByRole("combobox", { name: "Provider" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "review" })).toBeDisabled();
});

it("follows the running coordinator harness when it cannot change", () => {
  const onChange = vi.fn();
  render(
    <WorkflowPromptButtons
      agentName="workflow-controller"
      disabled={false}
      draft={
        'Prepare the start message for `review-fix` using the task below.\r\n\r\nUse workflow_draft provider="codex".\r\n\r\nMy task'
      }
      onChange={onChange}
      harness="claude-native"
    />,
  );
  expect(screen.queryByRole("combobox")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "implement" }));
  expect(onChange).toHaveBeenCalledWith(
    'Prepare the start message for `implement-review-fix` using the task below.\n\nUse workflow_draft provider="claude".\n\nMy task',
  );
});
