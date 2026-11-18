import { useState } from "react";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";
import { WorkflowPromptButtons } from "./WorkflowPromptButtons";

afterEach(cleanup);

function Editor({ initial = "My task", disabled = false }) {
  const [draft, setDraft] = useState(initial);
  return (
    <>
      <WorkflowPromptButtons
        agentName="workflow-controller"
        disabled={disabled}
        draft={draft}
        onChange={setDraft}
      />
      <output aria-label="Draft">{draft}</output>
    </>
  );
}

it("defaults to Codex and changes providers without duplicating the workflow or task", () => {
  render(<Editor />);
  const provider = screen.getByRole("combobox", { name: "Worker provider" });
  expect(provider).toHaveValue("codex");
  fireEvent.change(provider, { target: { value: "claude" } });
  expect(screen.getByLabelText("Draft")).toHaveTextContent("My task");
  for (const workflow of ["implement", "compare", "review"]) {
    fireEvent.click(screen.getByRole("button", { name: workflow }));
    fireEvent.click(screen.getByRole("button", { name: workflow }));
    expect(screen.getByLabelText("Draft").textContent).toBe(
      `Prepare the start message for \`${workflow === "review" ? "review-fix" : `${workflow}-review-fix`}\` using the task below.\n\nUse workflow_draft provider="claude".\n\nMy task`,
    );
  }
  fireEvent.change(provider, { target: { value: "codex" } });
  expect(screen.getByLabelText("Draft").textContent).toBe(
    'Prepare the start message for `review-fix` using the task below.\n\nUse workflow_draft provider="codex".\n\nMy task',
  );
});

it("restores the draft provider and disables the selector with the workflow controls", () => {
  render(
    <Editor
      initial={
        'Prepare the start message for `review-fix` using the task below.\r\n\r\nUse workflow_draft provider="claude".\r\n\r\nMy task'
      }
      disabled
    />,
  );
  expect(screen.getByRole("combobox", { name: "Worker provider" })).toHaveValue("claude");
  expect(screen.getByRole("combobox", { name: "Worker provider" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "review" })).toBeDisabled();
});
