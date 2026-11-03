import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { WorkflowStartButton } from "./WorkflowStartButton";
import { authenticatedFetch } from "@/lib/identity";

vi.mock("@/lib/identity", () => ({ authenticatedFetch: vi.fn() }));
afterEach(cleanup);

it("shows server-validated details and sends only the selected draft token", async () => {
  const draft = {
    token: "first",
    workflow: "implement-review-fix",
    repository: "/repo",
    task: "Task",
    baseline: "main",
    base: "main",
    branch: "feature/lcs-1670",
    publish: false,
  };
  vi.mocked(authenticatedFetch).mockImplementation(
    async () => new Response(JSON.stringify({ draft })),
  );
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const send = vi.fn();
  const view = (disabled: boolean) => (
    <QueryClientProvider client={client}>
      <WorkflowStartButton sessionId="session" disabled={disabled} onStart={send} />
    </QueryClientProvider>
  );
  const rendered = render(view(true));
  const button = await screen.findByRole("button", { name: "Start workflow" });
  expect(button).toBeDisabled();
  fireEvent.click(button);
  expect(send).not.toHaveBeenCalled();
  fireEvent.click(screen.getByText("Review prepared workflow"));
  expect(screen.getByText(/feature\/lcs-1670/)).toBeVisible();
  rendered.rerender(view(false));
  fireEvent.click(button);
  expect(send).toHaveBeenCalledWith("/workflow start-draft first");
  draft.token = "second";
  await client.invalidateQueries({ queryKey: ["workflow-draft", "session"] });
  fireEvent.click(await screen.findByRole("button", { name: "Start workflow" }));
  expect(send).toHaveBeenLastCalledWith("/workflow start-draft second");
  client.clear();
});

it("does not offer a start action without a saved draft", async () => {
  vi.mocked(authenticatedFetch).mockResolvedValue(new Response(JSON.stringify({ draft: null })));
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <WorkflowStartButton sessionId="empty" disabled={false} onStart={vi.fn()} />
    </QueryClientProvider>,
  );
  await client.fetchQuery({ queryKey: ["workflow-draft", "empty"] });
  expect(screen.queryByRole("button", { name: "Start workflow" })).toBeNull();
  client.clear();
});
