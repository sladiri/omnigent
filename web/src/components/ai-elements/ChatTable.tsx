import { CheckIcon, CopyIcon, Maximize2Icon } from "lucide-react";
import { useContext, useEffect, useRef, useState, type ComponentProps } from "react";
import {
  extractTableDataFromElement,
  StreamdownContext,
  TableDownloadDropdown,
  tableDataToCSV,
  tableDataToMarkdown,
  tableDataToTSV,
} from "streamdown";
import { Dialog, DialogContent, DialogTitle, DialogTrigger } from "@/components/ui/dialog";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { showToast } from "@/components/ui/toast";
import { copyText } from "@/lib/clipboard";
import { cn } from "@/lib/utils";

const ACTION_CLASS =
  "cursor-pointer p-1 text-muted-foreground transition-all hover:text-foreground disabled:cursor-not-allowed disabled:opacity-50";
const FORMATS = { Markdown: tableDataToMarkdown, CSV: tableDataToCSV, TSV: tableDataToTSV };

// Streamdown's built-in table copier requires navigator.clipboard.write, which
// is absent on LAN HTTP origins. Use the same fallback as chat/code copying.
export function ChatTable({
  children,
  className,
  node: _node,
  ...props
}: ComponentProps<"table"> & { node?: unknown }) {
  const { controls, isAnimating } = useContext(StreamdownContext);
  const tableControls = typeof controls === "object" ? controls.table : controls;
  const enabled = (action: "copy" | "download" | "fullscreen") =>
    tableControls !== false &&
    (typeof tableControls !== "object" || tableControls[action] !== false);
  const [copied, setCopied] = useState(false);
  const tableRef = useRef<HTMLTableElement>(null);
  const fullscreenTableRef = useRef<HTMLTableElement>(null);
  const timeout = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  useEffect(() => () => clearTimeout(timeout.current), []);

  async function copy(convert: typeof tableDataToMarkdown, table: HTMLTableElement | null) {
    if (!table) return;
    try {
      await copyText(convert(extractTableDataFromElement(table)), table.outerHTML);
      setCopied(true);
      clearTimeout(timeout.current);
      timeout.current = setTimeout(() => setCopied(false), 2000);
    } catch {
      showToast("Could not copy table. Select the table text and copy it manually.");
    }
  }

  function tableContent(fullscreen = false) {
    const Icon = copied ? CheckIcon : CopyIcon;
    return (
      <div
        className={cn("flex min-h-0 flex-col gap-2", fullscreen && "h-full")}
        data-streamdown="table-wrapper"
      >
        <div className={cn("flex items-center justify-end gap-1", fullscreen && "pr-10")}>
          {enabled("copy") && (
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <button
                  type="button"
                  title="Copy table"
                  className={ACTION_CLASS}
                  disabled={isAnimating}
                >
                  <Icon size={14} />
                </button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end">
                {Object.entries(FORMATS).map(([format, convert]) => (
                  <DropdownMenuItem
                    key={format}
                    onSelect={() =>
                      void copy(convert, (fullscreen ? fullscreenTableRef : tableRef).current)
                    }
                  >
                    {format}
                  </DropdownMenuItem>
                ))}
              </DropdownMenuContent>
            </DropdownMenu>
          )}
          {enabled("download") && <TableDownloadDropdown />}
          {!fullscreen && enabled("fullscreen") && (
            <DialogTrigger asChild>
              <button
                type="button"
                title="View fullscreen"
                className={ACTION_CLASS}
                disabled={isAnimating}
              >
                <Maximize2Icon size={14} />
              </button>
            </DialogTrigger>
          )}
        </div>
        <div
          className={cn(
            "overflow-auto rounded-md border border-border bg-background",
            fullscreen && "[&_thead]:sticky [&_thead]:top-0 [&_thead]:z-10",
          )}
        >
          <table
            ref={fullscreen ? fullscreenTableRef : tableRef}
            className={cn("w-full divide-y divide-border", className)}
            data-streamdown="table"
            {...props}
          >
            {children}
          </table>
        </div>
      </div>
    );
  }

  return (
    <Dialog>
      <div className="my-4 rounded-lg border border-border bg-sidebar p-2">{tableContent()}</div>
      <DialogContent
        className="flex h-dvh w-screen max-w-none flex-col rounded-none p-4 sm:max-w-none"
        aria-describedby={undefined}
      >
        <DialogTitle className="sr-only">Table</DialogTitle>
        {tableContent(true)}
      </DialogContent>
    </Dialog>
  );
}
