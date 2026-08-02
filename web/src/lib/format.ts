import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

export const cn = (...inputs: ClassValue[]) => twMerge(clsx(inputs));

const inr = new Intl.NumberFormat("en-IN", {
  style: "currency",
  currency: "INR",
  maximumFractionDigits: 2,
});
const inrCompact = new Intl.NumberFormat("en-IN", {
  style: "currency",
  currency: "INR",
  notation: "compact",
  maximumFractionDigits: 1,
});
const qtyFmt = new Intl.NumberFormat("en-IN", { maximumFractionDigits: 2 });

export const money = (value: string | number | null | undefined) =>
  value === null || value === undefined ? "—" : inr.format(Number(value));

/** Compact currency for dashboard tiles, where full precision is noise. */
export const moneyShort = (value: string | number | null | undefined) =>
  value === null || value === undefined ? "—" : inrCompact.format(Number(value));

export const qty = (value: string | number | null | undefined) =>
  value === null || value === undefined ? "—" : qtyFmt.format(Number(value));

export const num = (value: string | number | null | undefined) =>
  value === null || value === undefined ? 0 : Number(value);

export const date = (value: string | null | undefined) =>
  !value
    ? "—"
    : new Date(value).toLocaleDateString("en-IN", {
        day: "2-digit",
        month: "short",
        year: "numeric",
      });

export const dateTime = (value: string | null | undefined) =>
  !value
    ? "—"
    : new Date(value).toLocaleString("en-IN", {
        day: "2-digit",
        month: "short",
        hour: "2-digit",
        minute: "2-digit",
      });

/** "in 12 days" / "8 days ago" — used everywhere expiry is shown. */
export function relativeDays(days: number | null | undefined): string {
  if (days === null || days === undefined) return "—";
  if (days < 0) return `${Math.abs(days)}d expired`;
  if (days === 0) return "today";
  return `${days}d left`;
}

export const titleCase = (value: string) =>
  value
    .toLowerCase()
    .split("_")
    .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
    .join(" ");
