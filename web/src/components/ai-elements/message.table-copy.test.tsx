import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MessageResponse } from "./message";

const clipboardDescriptor = Object.getOwnPropertyDescriptor(Navigator.prototype, "clipboard");
const execDescriptor = Object.getOwnPropertyDescriptor(Document.prototype, "execCommand");
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  if (clipboardDescriptor)
    Object.defineProperty(Navigator.prototype, "clipboard", clipboardDescriptor);
  else delete (Navigator.prototype as { clipboard?: unknown }).clipboard;
  if (execDescriptor) Object.defineProperty(Document.prototype, "execCommand", execDescriptor);
  else delete (Document.prototype as { execCommand?: unknown }).execCommand;
});

const markdown = "| Name | Value |\n| --- | --- |\n| Example | 42 |";
const formats = [
  ["Markdown", markdown],
  ["CSV", "Name,Value\nExample,42"],
  ["TSV", "Name\tValue\nExample\t42"],
];
function clipboard(reject: boolean, succeeds = true) {
  const setData = vi.fn();
  Object.defineProperty(Navigator.prototype, "clipboard", {
    configurable: true,
    value: reject ? { write: vi.fn().mockRejectedValue(new Error("denied")) } : undefined,
  });
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

describe.each([false, true])("table clipboard rejected: %s", (reject) => {
  describe.each([false, true])("fullscreen: %s", (fullscreen) => {
    it.each(formats)("copies %s with HTML", async (format, expected) => {
      const setData = clipboard(reject);
      render(<MessageResponse>{markdown}</MessageResponse>);
      if (fullscreen) fireEvent.click(screen.getByRole("button", { name: "View fullscreen" }));
      const root = fullscreen ? within(screen.getByRole("dialog")) : screen;
      const html = root.getByRole("table").outerHTML;
      fireEvent.click(root.getByRole("button", { name: "Copy table" }));
      fireEvent.click(root.getByRole("button", { name: format }));
      await waitFor(() => expect(setData).toHaveBeenCalledWith("text/plain", expected));
      expect(setData).toHaveBeenCalledWith("text/html", html);
      expect(document.querySelector("textarea")).toBeNull();
    });
  });
});

it("shows failure when both clipboard paths fail", async () => {
  clipboard(true, false);
  render(<MessageResponse>{markdown}</MessageResponse>);
  fireEvent.click(screen.getByRole("button", { name: "Copy table" }));
  fireEvent.click(screen.getByRole("button", { name: "Markdown" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Could not copy table");
});
