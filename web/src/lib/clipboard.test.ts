import { afterEach, describe, expect, it, vi } from "vitest";

import { copyText } from "./clipboard";

const clipboardDescriptor = Object.getOwnPropertyDescriptor(Navigator.prototype, "clipboard");
const execCommandDescriptor = Object.getOwnPropertyDescriptor(Document.prototype, "execCommand");

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  document.body.innerHTML = "";
  document.getSelection()?.removeAllRanges();

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

describe("copyText", () => {
  it("writes plain text and HTML through the rich clipboard API", async () => {
    const write = vi.fn().mockResolvedValue(undefined);
    const items: Record<string, Blob>[] = [];
    vi.stubGlobal("ClipboardItem", function (data: Record<string, Blob>) {
      items.push(data);
    });
    Object.defineProperty(Navigator.prototype, "clipboard", {
      configurable: true,
      value: { write },
    });

    await copyText("table text", "<table></table>");

    expect(write).toHaveBeenCalledOnce();
    expect(items).toHaveLength(1);
    expect(items[0]["text/plain"]).toMatchObject({ type: "text/plain", size: 10 });
    expect(items[0]["text/html"]).toMatchObject({ type: "text/html", size: 15 });
    expect(document.querySelector("textarea")).toBeNull();
  });

  it("keeps fallback focus inside the active menu", async () => {
    Object.defineProperty(Navigator.prototype, "clipboard", {
      configurable: true,
      value: undefined,
    });
    const menu = document.createElement("div");
    menu.setAttribute("role", "menu");
    const item = document.createElement("button");
    menu.appendChild(item);
    document.body.appendChild(menu);
    item.focus();
    Object.defineProperty(Document.prototype, "execCommand", {
      configurable: true,
      value: vi.fn(() => {
        expect(document.activeElement?.tagName).toBe("TEXTAREA");
        expect(menu.contains(document.activeElement)).toBe(true);
        return true;
      }),
    });

    await copyText("table text", "<table></table>");

    expect(document.activeElement).toBe(item);
    expect(menu.querySelector("textarea")).toBeNull();
  });

  it("selects an off-screen textarea and writes exact text through the fallback copy event", async () => {
    const setData = vi.fn();
    const selectedTextAreas: HTMLTextAreaElement[] = [];
    const originalSelect = HTMLTextAreaElement.prototype.select;
    const originalFocus = HTMLTextAreaElement.prototype.focus;
    const selectedRange = document.createRange();
    const existingText = document.createTextNode("existing selection");
    const selectionContainer = document.createElement("p");
    const focusTarget = document.createElement("button");

    focusTarget.textContent = "keep focus";
    document.body.appendChild(focusTarget);
    focusTarget.focus();
    selectionContainer.appendChild(existingText);
    document.body.appendChild(selectionContainer);
    selectedRange.selectNodeContents(existingText);
    document.getSelection()?.removeAllRanges();
    document.getSelection()?.addRange(selectedRange);

    Object.defineProperty(Navigator.prototype, "clipboard", {
      configurable: true,
      value: undefined,
    });
    vi.spyOn(HTMLTextAreaElement.prototype, "focus").mockImplementation(function focus(
      this: HTMLTextAreaElement,
    ) {
      originalFocus.call(this);
    });
    vi.spyOn(HTMLTextAreaElement.prototype, "select").mockImplementation(function select(
      this: HTMLTextAreaElement,
    ) {
      selectedTextAreas.push(this);
      originalSelect.call(this);
    });
    Object.defineProperty(Document.prototype, "execCommand", {
      configurable: true,
      value: vi.fn((command: string) => {
        expect(command).toBe("copy");
        expect(selectedTextAreas).toHaveLength(1);
        expect(selectedTextAreas[0]?.value).toBe("first line\nsecond line");
        expect(selectedTextAreas[0]?.selectionStart).toBe(0);
        expect(selectedTextAreas[0]?.selectionEnd).toBe("first line\nsecond line".length);
        expect(document.body.contains(selectedTextAreas[0] ?? null)).toBe(true);

        const event = new Event("copy", {
          bubbles: true,
          cancelable: true,
        }) as ClipboardEvent;
        Object.defineProperty(event, "clipboardData", {
          configurable: true,
          value: { setData },
        });
        document.dispatchEvent(event);
        return true;
      }),
    });

    await expect(copyText("first line\nsecond line")).resolves.toBeUndefined();

    expect(setData).toHaveBeenCalledTimes(1);
    expect(setData).toHaveBeenCalledWith("text/plain", "first line\nsecond line");
    expect(document.querySelector("textarea")).toBeNull();
    expect(document.getSelection()?.rangeCount).toBe(1);
    expect(document.getSelection()?.toString()).toBe("existing selection");
    expect(document.activeElement).toBe(focusTarget);
  });

  it("falls back to selected-textarea copy when async clipboard rejects", async () => {
    const writeText = vi.fn().mockRejectedValue(new Error("permission denied"));
    const selectedTextAreas: HTMLTextAreaElement[] = [];

    Object.defineProperty(Navigator.prototype, "clipboard", {
      configurable: true,
      value: { writeText },
    });
    vi.spyOn(HTMLTextAreaElement.prototype, "select").mockImplementation(function select(
      this: HTMLTextAreaElement,
    ) {
      selectedTextAreas.push(this);
    });
    Object.defineProperty(Document.prototype, "execCommand", {
      configurable: true,
      value: vi.fn((command: string) => {
        expect(command).toBe("copy");
        expect(selectedTextAreas[0]?.value).toBe("fallback text");
        return true;
      }),
    });

    await expect(copyText("fallback text")).resolves.toBeUndefined();

    expect(writeText).toHaveBeenCalledWith("fallback text");
    expect(document.execCommand).toHaveBeenCalledWith("copy");
  });
});
