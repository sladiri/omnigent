export async function copyText(text: string, html?: string): Promise<void> {
  if (
    html !== undefined &&
    typeof navigator !== "undefined" &&
    navigator.clipboard?.write &&
    typeof ClipboardItem !== "undefined"
  ) {
    try {
      await navigator.clipboard.write([
        new ClipboardItem({
          "text/plain": new Blob([text], { type: "text/plain" }),
          "text/html": new Blob([html], { type: "text/html" }),
        }),
      ]);
      return;
    } catch {
      // Keep both formats when rich clipboard access is denied.
    }
  }
  if (html === undefined && typeof navigator !== "undefined" && navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text);
      return;
    } catch {
      // Fall through to the selected-textarea path when async clipboard is
      // unavailable at runtime, e.g. permission denied or a non-secure origin.
    }
  }

  if (copyTextWithExecCommand(text, html)) return;

  throw new Error("Clipboard API not available");
}

function copyTextWithExecCommand(text: string, html?: string): boolean {
  if (
    typeof document === "undefined" ||
    typeof document.execCommand !== "function" ||
    !document.body
  ) {
    return false;
  }

  const selection = document.getSelection();
  const previouslyFocused =
    document.activeElement instanceof HTMLElement ? document.activeElement : null;
  const selectedRanges = selection
    ? Array.from({ length: selection.rangeCount }, (_, index) => selection.getRangeAt(index))
    : [];
  const textArea = document.createElement("textarea");

  textArea.value = text;
  textArea.setAttribute("readonly", "");
  textArea.style.position = "fixed";
  textArea.style.top = "0";
  textArea.style.left = "0";
  textArea.style.width = "1px";
  textArea.style.height = "1px";
  textArea.style.padding = "0";
  textArea.style.border = "0";
  textArea.style.opacity = "0";
  textArea.style.pointerEvents = "none";

  const handleCopy = (event: ClipboardEvent) => {
    event.preventDefault();
    event.clipboardData?.setData("text/plain", text);
    if (html !== undefined) event.clipboardData?.setData("text/html", html);
  };

  document.addEventListener("copy", handleCopy);
  // Keep temporary focus inside the active menu or dialog's focus trap.
  (
    previouslyFocused?.closest('[role="dialog"], [role="menu"], dialog') ?? document.body
  ).appendChild(textArea);
  try {
    textArea.focus();
    textArea.select();
    textArea.selectionStart = 0;
    textArea.selectionEnd = textArea.value.length;

    return document.execCommand("copy");
  } finally {
    document.removeEventListener("copy", handleCopy);
    textArea.remove();
    if (previouslyFocused?.isConnected) {
      previouslyFocused.focus({ preventScroll: true });
    }
    if (selection) {
      selection.removeAllRanges();
      for (const range of selectedRanges) {
        selection.addRange(range);
      }
    }
  }
}
