import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Message, MessageAction, MessageActions, MessageContent, MessageResponse } from "./message";

const clipboardDescriptor = Object.getOwnPropertyDescriptor(Navigator.prototype, "clipboard");
const execCommandDescriptor = Object.getOwnPropertyDescriptor(Document.prototype, "execCommand");

afterEach(() => {
  vi.unstubAllGlobals();
  cleanup();
  vi.restoreAllMocks();
  if (clipboardDescriptor) {
    Object.defineProperty(Navigator.prototype, "clipboard", clipboardDescriptor);
  } else {
    delete (Navigator.prototype as { clipboard?: unknown }).clipboard;
  }

  if (execCommandDescriptor) {
    Object.defineProperty(Document.prototype, "execCommand", execCommandDescriptor);
  } else {
    delete (Document.prototype as { execCommand?: unknown }).execCommand;
  }
});

describe("MessageContent", () => {
  it("uses the settings-driven interface text token", () => {
    render(<MessageContent>Message text</MessageContent>);

    const content = screen.getByText("Message text");
    expect(content).toHaveClass("text-ui", "group-[.is-user]:px-3", "group-[.is-user]:py-2");
    expect(content).not.toHaveClass("text-[0.8125rem]", "leading-[1.125rem]");
    expect(content).not.toHaveClass("group-[.is-user]:px-4", "group-[.is-user]:py-3");
  });
});

describe("Message", () => {
  it("keeps the message shrinkable", () => {
    render(<Message data-testid="message" from="assistant" />);

    expect(screen.getByTestId("message")).toHaveClass("min-w-0");
  });

  it("keeps a caller's width override alongside min-w-0", () => {
    render(<Message className="max-w-3xl" data-testid="message" from="assistant" />);

    const message = screen.getByTestId("message");
    expect(message).toHaveClass("min-w-0", "max-w-3xl");
  });
});

describe("MessageAction", () => {
  it("uses muted color by default and foreground color on hover", () => {
    render(
      <MessageAction label="Copy">
        <svg aria-hidden />
      </MessageAction>,
    );

    expect(screen.getByRole("button", { name: "Copy" })).toHaveClass(
      "text-muted-foreground",
      "hover:text-foreground",
    );
  });
});

describe("MessageActions", () => {
  it("uses 12px spacing between actions", () => {
    render(<MessageActions data-testid="message-actions">Actions</MessageActions>);

    expect(screen.getByTestId("message-actions")).toHaveClass("gap-3");
  });
});

// Streamdown renders a diagram only once an IntersectionObserver reports it
// visible; report every observed element visible so diagrams render in jsdom.
class VisibleIntersectionObserver {
  private readonly callback: IntersectionObserverCallback;
  constructor(callback: IntersectionObserverCallback) {
    this.callback = callback;
  }
  observe(target: Element) {
    this.callback(
      [{ isIntersecting: true, target } as IntersectionObserverEntry],
      this as unknown as IntersectionObserver,
    );
  }
  unobserve() {}
  disconnect() {}
  takeRecords(): IntersectionObserverEntry[] {
    return [];
  }
}

describe("MessageResponse", () => {
  it("blocks external image markdown and renders a placeholder", async () => {
    render(<MessageResponse>{"![leak](https://attacker.example/pixel.png)"}</MessageResponse>);

    expect(document.querySelector('img[src^="https://attacker.example"]')).toBeNull();
    expect(await screen.findByText("[Image blocked: leak]")).toBeTruthy();
  });

  it("re-renders when rendering props change even if the text is unchanged", async () => {
    const { container, rerender } = render(
      <MessageResponse className="math-config-a">same text</MessageResponse>,
    );

    expect(container.firstElementChild).toHaveClass("math-config-a");

    rerender(<MessageResponse className="math-config-b">same text</MessageResponse>);

    await waitFor(() => {
      expect(container.firstElementChild).toHaveClass("math-config-b");
    });
  });

  it("explains an invalid mermaid fence instead of dumping the parser error", async () => {
    vi.stubGlobal("IntersectionObserver", VisibleIntersectionObserver);
    render(
      <MessageResponse>
        {
          "```mermaid\nsequenceDiagram\n    A->>B: hi\n    Note over A,B: once; twice\n    A=>B: again\n```"
        }
      </MessageResponse>,
    );

    const card = await screen.findByTestId("mermaid-error", {}, { timeout: 10_000 });
    expect(card.textContent).toContain("Mermaid couldn't parse line 3");
    expect(card.querySelector("code")?.textContent).toBe("Note over A,B: once; twice");
    expect(card.textContent).toContain("#59;");
  }, 15_000);

  it("gives prose a break opportunity for an unbroken run (OMNI-2900)", () => {
    const { container } = render(<MessageResponse>same text</MessageResponse>);

    expect(container.firstElementChild).toHaveClass("wrap-anywhere");
  });

  it("keeps wrap-anywhere alongside a caller-supplied className", () => {
    const { container } = render(
      <MessageResponse className="math-config-a">same text</MessageResponse>,
    );

    expect(container.firstElementChild).toHaveClass("wrap-anywhere", "math-config-a");
  });
});

describe("MessageResponse code-block copy", () => {
  it("copies the exact fenced code text through the fallback path", async () => {
    const copiedText: string[] = [];
    Object.defineProperty(Navigator.prototype, "clipboard", {
      configurable: true,
      value: undefined,
    });
    Object.defineProperty(Document.prototype, "execCommand", {
      configurable: true,
      value: vi.fn((command: string) => {
        expect(command).toBe("copy");
        const event = new Event("copy", {
          bubbles: true,
          cancelable: true,
        }) as ClipboardEvent;
        Object.defineProperty(event, "clipboardData", {
          configurable: true,
          value: {
            setData: (type: string, value: string) => {
              expect(type).toBe("text/plain");
              copiedText.push(value);
            },
          },
        });
        document.dispatchEvent(event);
        return true;
      }),
    });

    render(
      <MessageResponse>{"```ts\nconst value = 1;\nconsole.log(value);\n```"}</MessageResponse>,
    );

    fireEvent.click(await screen.findByRole("button", { name: "Copy Code" }));

    await waitFor(() => {
      expect(copiedText).toEqual(["const value = 1;\nconsole.log(value);\n"]);
    });
    expect(screen.getByRole("button", { name: "Download file" })).toBeInTheDocument();
  });
});

describe("MessageResponse table copy", () => {
  const markdown = "| Name | Value |\n| --- | --- |\n| Example | 42 |";
  const formats = [
    ["Markdown", markdown],
    ["CSV", "Name,Value\nExample,42"],
    ["TSV", "Name\tValue\nExample\t42"],
  ];

  function fallbackClipboard(rejected: boolean, succeeds = true) {
    const setData = vi.fn();
    vi.spyOn(navigator, "clipboard", "get").mockReturnValue(
      (rejected
        ? { write: vi.fn().mockRejectedValue(new Error("denied")) }
        : undefined) as unknown as Clipboard,
    );
    vi.stubGlobal("ClipboardItem", class {});
    Object.defineProperty(Document.prototype, "execCommand", {
      configurable: true,
      value: vi.fn(() => {
        if (!succeeds) return false;
        const event = new Event("copy", { bubbles: true, cancelable: true });
        Object.defineProperty(event, "clipboardData", { value: { setData } });
        document.dispatchEvent(event);
        return true;
      }),
    });
    return setData;
  }

  describe.each([false, true])("clipboard rejected: %s", (rejected) => {
    it.each(formats)("copies %s and HTML through the fallback", async (format, expected) => {
      const user = userEvent.setup();
      const setData = fallbackClipboard(rejected);
      render(<MessageResponse>{markdown}</MessageResponse>);
      const html = screen.getByRole("table").outerHTML;

      await user.click(screen.getByRole("button", { name: "Copy table" }));
      await user.click(screen.getByRole("menuitem", { name: format }));

      await waitFor(() => expect(setData).toHaveBeenCalledWith("text/plain", expected));
      expect(setData).toHaveBeenCalledWith("text/html", html);
      expect(screen.queryByRole("menuitem")).not.toBeInTheDocument();
      expect(document.querySelector("textarea")).toBeNull();
    });
  });

  it("copies the selected table from fullscreen", async () => {
    const user = userEvent.setup();
    const setData = fallbackClipboard(false);
    render(
      <MessageResponse>
        {markdown + "\n\n" + markdown.replace("Example", "Second")}
      </MessageResponse>,
    );
    await user.click(screen.getAllByRole("button", { name: "View fullscreen" })[1]);
    const dialog = screen.getByRole("dialog", { name: "Table" });
    await user.click(within(dialog).getByRole("button", { name: "Copy table" }));
    await user.click(screen.getByRole("menuitem", { name: "Markdown" }));

    await waitFor(() =>
      expect(setData).toHaveBeenCalledWith("text/plain", markdown.replace("Example", "Second")),
    );
    expect(setData).toHaveBeenCalledWith("text/html", within(dialog).getByRole("table").outerHTML);
    expect(within(dialog).getByRole("button", { name: "Download table" })).toBeInTheDocument();
  });

  it("reports a clipboard failure", async () => {
    const user = userEvent.setup();
    fallbackClipboard(false, false);
    const dispatch = vi.spyOn(window, "dispatchEvent");
    render(<MessageResponse>{markdown}</MessageResponse>);
    await user.click(screen.getByRole("button", { name: "Copy table" }));
    await user.click(screen.getByRole("menuitem", { name: "Markdown" }));

    await waitFor(() =>
      expect(dispatch).toHaveBeenCalledWith(
        expect.objectContaining({
          type: "omnigent:toast",
          detail: { content: "Could not copy table. Select the table text and copy it manually." },
        }),
      ),
    );
  });

  it("honors table controls and disables actions during streaming", () => {
    const { rerender } = render(<MessageResponse isAnimating>{markdown}</MessageResponse>);
    expect(screen.getByRole("button", { name: "Copy table" })).toBeDisabled();
    rerender(
      <MessageResponse controls={{ table: { copy: false, fullscreen: false } }}>
        {markdown}
      </MessageResponse>,
    );
    expect(screen.queryByRole("button", { name: "Copy table" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "View fullscreen" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Download table" })).toBeInTheDocument();
  });
});
