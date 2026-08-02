import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "./api";

/**
 * The value, but only after it has stopped changing for `delay` ms.
 *
 * Search boxes feed straight into a query key, so without this every keystroke
 * is a round trip — and the responses can land out of order, briefly showing
 * results for a prefix of what was typed.
 */
export function useDebounced<T>(value: T, delay = 300): T {
  const [settled, setSettled] = useState(value);

  useEffect(() => {
    const timer = setTimeout(() => setSettled(value), delay);
    return () => clearTimeout(timer);
  }, [value, delay]);

  return settled;
}

/**
 * Which capabilities an administrator has switched on.
 *
 * Used to hide navigation entries that would 404. It is deliberately not a
 * security boundary — the server enforces the same switches on every request —
 * so it is safe to read for any signed-in user and safe to cache for a while.
 */
export function useFeatures() {
  return useQuery({
    queryKey: ["features"],
    queryFn: () => api.get<{ enabled: Record<string, boolean> }>(
      "/api/v1/settings/features",
    ),
    staleTime: 60_000,
  });
}
