import { AlertTriangle, ChevronLeft, ChevronRight, Inbox, Loader2 } from "lucide-react";
import type { HTMLAttributes, ReactNode, TdHTMLAttributes, ThHTMLAttributes } from "react";
import { Button } from "@/components/ui/button";
import { cn } from "@/utils/cn";

export function Panel({
  title,
  actions,
  className,
  children,
  bodyClassName,
}: {
  title?: ReactNode;
  actions?: ReactNode;
  className?: string;
  bodyClassName?: string;
  children: ReactNode;
}) {
  return (
    <section className={cn("panel flex flex-col", className)}>
      {(title || actions) && (
        <header className="flex items-center justify-between gap-3 px-5 pt-4 pb-3">
          {title && <h2 className="panel-title">{title}</h2>}
          {actions}
        </header>
      )}
      <div className={cn("flex-1 px-5 pb-5", !title && !actions && "pt-5", bodyClassName)}>
        {children}
      </div>
    </section>
  );
}

const tones = {
  neutral: "bg-surface-3 text-muted border-border-strong",
  primary: "bg-primary/15 text-blue-300 border-primary/30",
  success: "bg-success/15 text-green-300 border-success/30",
  warning: "bg-warning/15 text-amber-300 border-warning/30",
  danger: "bg-danger/15 text-red-300 border-danger/30",
  violet: "bg-violet/15 text-violet-300 border-violet/30",
} as const;

export type Tone = keyof typeof tones;

export function Badge({
  tone = "neutral",
  className,
  ...props
}: HTMLAttributes<HTMLSpanElement> & { tone?: Tone }) {
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 rounded-md border px-2 py-0.5 text-xs font-medium whitespace-nowrap",
        tones[tone],
        className,
      )}
      {...props}
    />
  );
}

export function StatusDot({ tone }: { tone: "success" | "warning" | "danger" | "neutral" }) {
  const color = {
    success: "bg-success shadow-[0_0_8px] shadow-success/60",
    warning: "bg-warning shadow-[0_0_8px] shadow-warning/60",
    danger: "bg-danger shadow-[0_0_8px] shadow-danger/60",
    neutral: "bg-subtle",
  }[tone];
  return <span className={cn("inline-block size-2 shrink-0 rounded-full", color)} />;
}

export function Spinner({ label = "Loading…" }: { label?: string }) {
  return (
    <div className="flex items-center justify-center gap-2 py-10 text-sm text-muted" role="status">
      <Loader2 className="size-4 animate-spin" /> {label}
    </div>
  );
}

export function EmptyState({
  title,
  description,
  icon,
}: {
  title: string;
  description?: string;
  icon?: ReactNode;
}) {
  return (
    <div className="flex flex-col items-center justify-center gap-2 py-12 text-center">
      <div className="text-subtle">{icon ?? <Inbox className="size-8" />}</div>
      <p className="text-sm font-medium text-fg">{title}</p>
      {description && <p className="max-w-sm text-xs text-muted">{description}</p>}
    </div>
  );
}

export function ErrorState({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div className="flex flex-col items-center gap-3 py-10 text-center" role="alert">
      <AlertTriangle className="size-6 text-danger" />
      <p className="text-sm text-red-300">{message}</p>
      {onRetry && (
        <Button variant="secondary" size="sm" onClick={onRetry}>
          Retry
        </Button>
      )}
    </div>
  );
}

export function Alert({
  tone = "danger",
  children,
}: {
  tone?: "danger" | "warning" | "success" | "primary";
  children: ReactNode;
}) {
  return (
    <div
      role="alert"
      className={cn("rounded-lg border px-3 py-2 text-sm", {
        "border-danger/30 bg-danger/10 text-red-300": tone === "danger",
        "border-warning/30 bg-warning/10 text-amber-200": tone === "warning",
        "border-success/30 bg-success/10 text-green-300": tone === "success",
        "border-primary/30 bg-primary/10 text-blue-200": tone === "primary",
      })}
    >
      {children}
    </div>
  );
}

export function Pagination({
  page,
  pageSize,
  total,
  onPage,
}: {
  page: number;
  pageSize: number;
  total: number;
  onPage: (page: number) => void;
}) {
  const pages = Math.max(1, Math.ceil(total / pageSize));
  const from = total === 0 ? 0 : (page - 1) * pageSize + 1;
  const to = Math.min(total, page * pageSize);
  return (
    <div className="flex items-center justify-between border-t border-border px-4 py-3 text-xs text-muted">
      <span>
        Showing {from}–{to} of {total}
      </span>
      <div className="flex items-center gap-2">
        <Button
          variant="ghost"
          size="icon"
          aria-label="Previous page"
          disabled={page <= 1}
          onClick={() => onPage(page - 1)}
        >
          <ChevronLeft />
        </Button>
        <span>
          Page {page} of {pages}
        </span>
        <Button
          variant="ghost"
          size="icon"
          aria-label="Next page"
          disabled={page >= pages}
          onClick={() => onPage(page + 1)}
        >
          <ChevronRight />
        </Button>
      </div>
    </div>
  );
}

export function Table({ className, ...props }: HTMLAttributes<HTMLTableElement>) {
  return (
    <div className="overflow-x-auto">
      <table className={cn("w-full border-collapse text-sm", className)} {...props} />
    </div>
  );
}

export function Th({ className, ...props }: ThHTMLAttributes<HTMLTableCellElement>) {
  return (
    <th
      className={cn(
        "border-b border-border px-4 py-2.5 text-left text-xs font-medium tracking-wide text-muted uppercase",
        className,
      )}
      {...props}
    />
  );
}

export function Td({ className, ...props }: TdHTMLAttributes<HTMLTableCellElement>) {
  return (
    <td className={cn("border-b border-border/60 px-4 py-3 align-middle", className)} {...props} />
  );
}
