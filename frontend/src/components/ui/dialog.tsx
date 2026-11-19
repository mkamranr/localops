import * as RadixDialog from "@radix-ui/react-dialog";
import { X } from "lucide-react";
import type { ReactNode } from "react";

export function Dialog({
  open,
  onOpenChange,
  title,
  description,
  children,
  footer,
  wide = false,
}: {
  wide?: boolean;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  description?: string;
  children: ReactNode;
  footer?: ReactNode;
}) {
  return (
    <RadixDialog.Root open={open} onOpenChange={onOpenChange}>
      <RadixDialog.Portal>
        <RadixDialog.Overlay className="fixed inset-0 z-40 bg-black/60 backdrop-blur-sm" />
        <RadixDialog.Content className={`panel fixed top-1/2 left-1/2 z-50 max-h-[90vh] ${wide ? "w-[min(95vw,56rem)]" : "w-[min(95vw,32rem)]"} -translate-x-1/2 -translate-y-1/2 overflow-y-auto shadow-2xl shadow-black/50`}>
          <div className="flex items-start justify-between gap-4 border-b border-border px-5 py-4">
            <div>
              <RadixDialog.Title className="text-base font-semibold">{title}</RadixDialog.Title>
              {description ? (
                <RadixDialog.Description className="mt-1 text-xs text-muted">
                  {description}
                </RadixDialog.Description>
              ) : (
                <RadixDialog.Description className="sr-only">{title}</RadixDialog.Description>
              )}
            </div>
            <RadixDialog.Close className="rounded-md p-1 text-muted hover:bg-surface-2 hover:text-fg">
              <X className="size-4" />
              <span className="sr-only">Close</span>
            </RadixDialog.Close>
          </div>
          <div className="space-y-4 px-5 py-4">{children}</div>
          {footer && (
            <div className="flex justify-end gap-2 border-t border-border px-5 py-3">{footer}</div>
          )}
        </RadixDialog.Content>
      </RadixDialog.Portal>
    </RadixDialog.Root>
  );
}
