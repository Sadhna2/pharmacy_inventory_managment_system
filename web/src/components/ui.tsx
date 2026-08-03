/**
 * UI primitives.
 *
 * Deliberately small and unstyled-by-default: one Button, one Badge, one Card,
 * one Field. Every screen composes these rather than inventing its own, which
 * is what keeps ~15 screens looking like one product.
 */

import {
  forwardRef,
  useEffect,
  type ButtonHTMLAttributes,
  type InputHTMLAttributes,
  type ReactNode,
  type SelectHTMLAttributes,
  type TextareaHTMLAttributes,
} from "react";
import { Loader2, X } from "lucide-react";
import { cn } from "@/lib/format";

/* ------------------------------------------------------------------ Button */

type Variant = "primary" | "secondary" | "ghost" | "danger";
type Size = "sm" | "md";

const VARIANTS: Record<Variant, string> = {
  primary:
    "bg-brand text-white hover:bg-brand-hover shadow-xs disabled:hover:bg-brand",
  secondary:
    "bg-surface text-ink border border-line hover:bg-muted hover:border-line-strong",
  ghost: "text-ink-soft hover:bg-muted hover:text-ink",
  danger: "bg-danger text-white hover:brightness-95 shadow-xs",
};

const SIZES: Record<Size, string> = {
  sm: "h-8 px-3 text-[13px] gap-1.5",
  md: "h-9.5 px-4 text-sm gap-2",
};

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant;
  size?: Size;
  loading?: boolean;
}

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(
  (
    { variant = "secondary", size = "md", loading, className, children, disabled, ...rest },
    ref,
  ) => (
    <button
      ref={ref}
      disabled={disabled || loading}
      className={cn(
        "inline-flex items-center justify-center rounded-lg font-medium",
        "transition-colors duration-150 select-none whitespace-nowrap",
        "disabled:opacity-45 disabled:cursor-not-allowed",
        VARIANTS[variant],
        SIZES[size],
        className,
      )}
      {...rest}
    >
      {loading && <Loader2 className="size-3.5 animate-spin" />}
      {children}
    </button>
  ),
);
Button.displayName = "Button";

/* ------------------------------------------------------------------- Badge */

type Tone = "neutral" | "ok" | "warn" | "danger" | "info" | "brand";

const TONES: Record<Tone, string> = {
  neutral: "bg-muted text-ink-soft ring-line",
  ok: "bg-ok-soft text-ok ring-ok/20",
  warn: "bg-warn-soft text-warn ring-warn/25",
  danger: "bg-danger-soft text-danger ring-danger/20",
  info: "bg-info-soft text-info ring-info/20",
  brand: "bg-brand-soft text-brand ring-brand/20",
};

export function Badge({
  tone = "neutral",
  children,
  className,
}: {
  tone?: Tone;
  children: ReactNode;
  className?: string;
}) {
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 rounded-md px-1.5 py-0.5",
        "text-[11px] font-medium ring-1 ring-inset whitespace-nowrap",
        TONES[tone],
        className,
      )}
    >
      {children}
    </span>
  );
}

/**
 * Document and stock statuses share one colour language across every screen.
 *
 * Green means finished — nothing further is expected of anyone. That is the
 * whole rule, and it is why APPROVED is amber rather than green: an approved
 * order is authorised but the goods have not arrived, which is the state that
 * most needs chasing. Painting it the same green as RECEIVED made "waiting on
 * a delivery" and "delivery complete" indistinguishable at a glance, so a
 * purchasing screen full of green told you nothing about what to do next.
 *
 *   neutral  nothing has started
 *   info     with someone else, or in motion — no action here yet
 *   amber    unfinished and yours to move
 *   green    done
 *   red      stopped or unsellable
 */
export function StatusBadge({ status }: { status: string }) {
  const tone: Tone =
    (
      {
        AVAILABLE: "ok",
        COMPLETED: "ok",
        RECEIVED: "ok",
        QUARANTINE: "danger",
        DAMAGED: "danger",
        CANCELLED: "danger",
        IN_TRANSIT: "info",
        SHIPPED: "info",
        ALLOCATED: "info",
        PENDING_APPROVAL: "info", // sitting with an approver, not with you
        APPROVED: "warn", // authorised — now waiting on the goods
        PARTIALLY_RECEIVED: "warn", // some arrived, the rest is outstanding
        RETURNED_PENDING: "warn",
        DRAFT: "neutral",
      } as Record<string, Tone>
    )[status] ?? "neutral";

  return (
    <Badge tone={tone}>
      {status.replace(/_/g, " ").toLowerCase().replace(/^./, (c) => c.toUpperCase())}
    </Badge>
  );
}

/* -------------------------------------------------------------------- Card */

export function Card({
  children,
  className,
}: {
  children: ReactNode;
  className?: string;
}) {
  return <div className={cn("card", className)}>{children}</div>;
}

export function CardHeader({
  title,
  description,
  actions,
}: {
  title: ReactNode;
  description?: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <div className="flex flex-wrap items-start justify-between gap-3 border-b border-line px-4 py-3 sm:px-5">
      <div className="min-w-0">
        <h2 className="text-[15px] font-semibold tracking-tight text-ink">
          {title}
        </h2>
        {description && (
          <p className="mt-0.5 text-[13px] text-ink-soft">{description}</p>
        )}
      </div>
      {actions && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
    </div>
  );
}

/* ------------------------------------------------------------------- Field */

export function Field({
  label,
  error,
  hint,
  required,
  children,
}: {
  label: string;
  error?: string;
  hint?: string;
  required?: boolean;
  children: ReactNode;
}) {
  return (
    <label className="block">
      <span className="mb-1.5 block text-[13px] font-medium text-ink">
        {label}
        {required && <span className="ml-0.5 text-danger">*</span>}
      </span>
      {children}
      {error ? (
        <span className="mt-1 block text-xs text-danger">{error}</span>
      ) : hint ? (
        <span className="mt-1 block text-xs text-ink-faint">{hint}</span>
      ) : null}
    </label>
  );
}

const controlClasses = cn(
  "w-full rounded-lg border border-line bg-surface px-3 text-sm text-ink",
  "placeholder:text-ink-faint transition-shadow",
  "focus:border-brand focus:ring-3 focus:ring-brand-ring focus:outline-none",
  "disabled:bg-muted disabled:text-ink-faint",
);

export const Input = forwardRef<HTMLInputElement, InputHTMLAttributes<HTMLInputElement>>(
  ({ className, ...rest }, ref) => (
    <input ref={ref} className={cn(controlClasses, "h-9.5", className)} {...rest} />
  ),
);
Input.displayName = "Input";

export const Textarea = forwardRef<
  HTMLTextAreaElement,
  TextareaHTMLAttributes<HTMLTextAreaElement>
>(({ className, ...rest }, ref) => (
  <textarea
    ref={ref}
    rows={2}
    className={cn(controlClasses, "resize-y py-2 leading-relaxed", className)}
    {...rest}
  />
));
Textarea.displayName = "Textarea";

export const Select = forwardRef<
  HTMLSelectElement,
  SelectHTMLAttributes<HTMLSelectElement>
>(({ className, children, ...rest }, ref) => (
  <select ref={ref} className={cn(controlClasses, "h-9.5", className)} {...rest}>
    {children}
  </select>
));
Select.displayName = "Select";

/* ------------------------------------------------------------------- Modal */

export function Modal({
  open,
  onClose,
  title,
  description,
  children,
  footer,
  wide,
}: {
  open: boolean;
  onClose: () => void;
  title: string;
  description?: string;
  children: ReactNode;
  footer?: ReactNode;
  /** `"xl"` for the goods receipt, whose seven line columns outgrow `wide`. */
  wide?: boolean | "xl";
}) {
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    document.addEventListener("keydown", onKey);
    // Prevent the page behind the modal from scrolling.
    const previous = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = previous;
    };
  }, [open, onClose]);

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-end justify-center sm:items-center sm:p-4">
      <div
        className="absolute inset-0 bg-ink/25 backdrop-blur-[2px]"
        onClick={onClose}
        aria-hidden
      />
      {/* Sheet on mobile, centred dialog on larger screens. */}
      <div
        role="dialog"
        aria-modal="true"
        className={cn(
          "relative flex max-h-[92dvh] w-full flex-col overflow-hidden bg-surface shadow-2xl",
          "rounded-t-2xl sm:rounded-xl",
          // Document forms carry up to five line columns beside the product
          // name; 3xl squeezed the name down to an initial. A receipt that can
          // cross-dock carries seven and needs more still — below that width
          // its line table scrolls sideways rather than compressing.
          wide === "xl" ? "sm:max-w-6xl" : wide ? "sm:max-w-4xl" : "sm:max-w-lg",
        )}
      >
        <div className="flex items-start justify-between gap-3 border-b border-line px-5 py-3.5">
          <div className="min-w-0">
            <h3 className="text-[15px] font-semibold tracking-tight">{title}</h3>
            {description && (
              <p className="mt-0.5 text-[13px] text-ink-soft">{description}</p>
            )}
          </div>
          <button
            onClick={onClose}
            aria-label="Close"
            className="-mr-1 rounded-md p-1 text-ink-faint transition-colors hover:bg-muted hover:text-ink"
          >
            <X className="size-4" />
          </button>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">{children}</div>
        {footer && (
          <div className="flex justify-end gap-2 border-t border-line bg-muted/40 px-5 py-3">
            {footer}
          </div>
        )}
      </div>
    </div>
  );
}

/* ---------------------------------------------------------------- feedback */

export function EmptyState({
  icon: Icon,
  title,
  description,
  action,
}: {
  icon?: React.ComponentType<{ className?: string }>;
  title: string;
  description?: string;
  action?: ReactNode;
}) {
  return (
    <div className="flex flex-col items-center justify-center px-6 py-14 text-center">
      {Icon && (
        <div className="mb-3 rounded-full bg-muted p-3">
          <Icon className="size-5 text-ink-faint" />
        </div>
      )}
      <p className="text-sm font-medium text-ink">{title}</p>
      {description && (
        <p className="mt-1 max-w-sm text-[13px] text-ink-soft">{description}</p>
      )}
      {action && <div className="mt-4">{action}</div>}
    </div>
  );
}

export function ErrorState({ error }: { error: unknown }) {
  const message =
    error instanceof Error ? error.message : "Something went wrong";
  return (
    <div className="m-4 rounded-lg border border-danger/20 bg-danger-soft px-4 py-3">
      <p className="text-sm font-medium text-danger">{message}</p>
    </div>
  );
}

export function Spinner({ className }: { className?: string }) {
  return (
    <Loader2 className={cn("size-4 animate-spin text-ink-faint", className)} />
  );
}

/** Shimmer rows that match the table's rhythm, so loading doesn't jump. */
export function TableSkeleton({ rows = 6 }: { rows?: number }) {
  return (
    <div className="divide-y divide-line">
      {Array.from({ length: rows }, (_, i) => (
        <div key={i} className="flex items-center gap-4 px-4 py-3.5">
          <div className="h-3 w-1/4 animate-pulse rounded bg-muted" />
          <div className="h-3 w-1/3 animate-pulse rounded bg-muted" />
          <div className="ml-auto h-3 w-16 animate-pulse rounded bg-muted" />
        </div>
      ))}
    </div>
  );
}
